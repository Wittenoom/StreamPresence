import time
from queue import Queue, Empty
from threading import Lock, Event
import src.provider as provider
import statistics
import src.LRCHelpers as LRCHelpers
from spotipy import Spotify

# --- Spotify Client Helper ---
def get_spotify_client(oauth, token_lock: Lock):
    """Return a Spotify client, refreshing token if needed."""
    with token_lock:
        token_info = oauth.get_cached_token()
        if not token_info:
            return None
        if oauth.is_token_expired(token_info):
            print("[INFO] Token expired. Refreshing...")
            token_info = oauth.refresh_access_token(token_info['refresh_token'])
        return Spotify(auth_manager=oauth)


# --- SentenceTransformer Helper ---
def encode_and_similar(arg1, arg2, model):
    """Compute similarity between two strings using a SentenceTransformer model."""
    if model:
        emb1 = model.encode(arg1)
        emb2 = model.encode(arg2)
        similarity = model.similarity(emb1, emb2)
        return similarity.tolist()[0]
    return None


# --- Lyric Sync Worker ---
def lyric_sync_worker(sync_queue: Queue, stop_event: Event):
    """Worker to print synced lyrics to console."""
    print("[SYNC] Lyric worker started")
    parsed_lyrics = None
    track_duration = None
    last_line_time = None
    start_ns = 0

    while True:
        if stop_event.is_set():
            break

        try:
            data = sync_queue.get_nowait()
        except Empty:
            data = None

        if data:
            parsed_lyrics = data["parsed_lyrics"]
            track_duration = data["track_duration"]
            progress_ms = data["progress"]
            start_ns = time.perf_counter_ns() - progress_ms * 1_000_000

        if not parsed_lyrics or start_ns is None:
            continue

        elapsed_ms = (time.perf_counter_ns() - start_ns) // 1_000_000
        if elapsed_ms >= track_duration:
            print("[SYNC] Song ended")
            stop_event.set()
            break

        line = LRCHelpers.get_synced_line(parsed_lyrics, int(elapsed_ms))
        if line and line["time"] != last_line_time:
            last_line_time = line["time"]
            print(line["text"], flush=True)


# --- Presence Sync Worker ---
def presence_sync_worker(sync_queue: Queue, stop_event: Event, rpc):
    """Worker to update Discord presence with current lyrics."""
    print("[SYNC] Presence worker started")
    parsed_lyrics = None
    track_duration = None
    progress_ms = None
    album_name = None
    window_ms = 15000
    last_update_ms = 0
    first_update = True
    start_ns = 0
    last_line = None

    while True:
        if stop_event.is_set():
            break

        try:
            data = sync_queue.get_nowait()
        except Empty:
            data = None

        if data:
            parsed_lyrics = data["parsed_lyrics"]
            track_duration = data["track_duration"]
            progress_ms = data["progress"]
            album_name = data["album_name"]
            start_ns = time.perf_counter_ns() - progress_ms * 1_000_000

        if not parsed_lyrics or start_ns is None:
            continue

        elapsed_ms = (time.perf_counter_ns() - start_ns) // 1_000_000
        if elapsed_ms >= track_duration:
            print("[SYNC] Song ended")
            stop_event.set()
            break

        if first_update or (elapsed_ms - last_update_ms >= window_ms):
            last_update_ms = elapsed_ms
            first_update = False
            window_end = elapsed_ms + window_ms

            future_lines = [
                str(line["text"]).replace("\n", "") or "🎵"
                for line in parsed_lyrics
                if elapsed_ms <= line["time"] <= window_end
            ]

            state = (f"{last_line}. " if last_line else "") + ". ".join(future_lines)
            if len(state) > 100:
                state = state[:100] + "..."
            if future_lines:
                last_line = future_lines[-1]

            now = time.time()
            progress_s = progress_ms / 1000
            start = int(now - progress_s)
            end = int(start + track_duration / 1000)

            rpc.update_presence(
                state=state or "🎵",
                start=start,
                end=end,
                large_image="spotify_app_logo_svg",
                large_text=album_name,
                details="Lyrics"
            )


# --- Polling Worker ---
def polling_worker(
    oauth,
    token_lock,
    rpc,
    lyrics_cache: dict,
    sync_queue: Queue,
    executor,
    lyric_sync_worker,
    presence_sync_worker,
    model,
    stop_event: Event,
    sync_future_ref: dict,
    presence_future_ref: dict,
    sync_stop_event: Event,
):
    """
    Main Spotify polling loop to manage lyrics and Discord presence.
    - sync_future_ref and presence_future_ref are dicts: {'future': None}
    - sync_stop_event is a persistent Event object shared with all active workers
    """
    last_track_id = None
    parsed_lyrics = None

    while not stop_event.is_set():
        sp = get_spotify_client(oauth, token_lock)
        try:
            track_info = sp.current_user_playing_track()
            if not track_info or not track_info.get("is_playing"):
                # paused or stopped
                if last_track_id is not None:
                    print("[SYNC] Paused or stopped - stopping workers")
                    sync_stop_event.clear()
                    sync_stop_event.set()  # tell workers to exit
                    last_track_id = None
                    parsed_lyrics = None

                    rpc.update_presence(
                        details="⏸️ Nothing playing",
                        large_image="spotify_app_logo_svg",
                        start=int(time.time())
                    )
                time.sleep(1)
                continue

            # current track info
            item = track_info["item"]
            track_id = item["id"]
            name = item["name"]
            artist = item["artists"][0]["name"]
            album = item["album"]["name"]
            duration = item["duration_ms"]

            # new song detected
            if track_id != last_track_id:
                print(f"[SYNC] New song detected: {name} - {artist}")

                # stop old workers
                sync_stop_event.clear()
                sync_stop_event.set()

                rpc.update_presence(
                    details="Now playing",
                    state=f"{name} - {artist}"[:255],
                    large_image="spotify_app_logo_svg",
                    large_text=album,
                    start=int(time.time())
                )
                last_track_id = track_id
                # get lyrics
                if track_id in lyrics_cache:
                    parsed_lyrics = lyrics_cache[track_id]["parsed"]
                else:
                    results = provider.get_lyrics(f"{name} - {artist}")
                    scored_songs = []
                    for song in results:
                        score = (
                            encode_and_similar(name, song["name"], model)[0] * 100 +
                            encode_and_similar(artist, song["artistName"], model)[0] * 90 +
                            encode_and_similar(album, song["albumName"], model)[0] * 80 -
                            abs(duration // 1000 - song["duration"]) * 15
                        )
                        scored_songs.append({
                            "score": score,
                            "trackname": song["trackName"],
                            "lyrics": song.get("syncedLyrics")
                        })

                    sorted_songs = sorted(scored_songs, key=lambda s: s["score"], reverse=True)
                    q1, q2, q3 = statistics.quantiles([s["score"] for s in sorted_songs], n=4, method="inclusive")
                    lrc = None
                    for s in sorted_songs:
                        if s["score"] < 0 or s["score"] < q3:
                            continue
                        if s.get("lyrics"):
                            lrc = s["lyrics"]
                            print(f"[DEBUG] Found lyrics for: {s['trackname']} (score: {s['score']:.2f})")
                            break

                    parsed_lyrics = LRCHelpers.parse_lrc(lrc) if lrc else None
                    lyrics_cache[track_id] = {"parsed": parsed_lyrics, "raw": lrc, "timestamp": time.time()}

            # mid-song sync
            if parsed_lyrics:
                progress = min(track_info["progress_ms"] + 150, duration)

                if track_info and track_info.get("is_playing"):
                    sync_queue.put({
                        "parsed_lyrics": parsed_lyrics,
                        "progress": progress,
                        "track_duration": duration,
                        "album_name": album
                    })

                # start workers if not running
                if not sync_future_ref.get("sync_future") or sync_future_ref["sync_future"].done():
                    sync_stop_event.clear()
                    sync_future_ref["sync_future"] = executor.submit(
                        lyric_sync_worker, sync_queue, sync_stop_event
                    )
                    presence_future_ref["presence_future"] = executor.submit(
                        presence_sync_worker, sync_queue, sync_stop_event, rpc
                    )

        except Exception as e:
            msg = str(e).lower()
            if "access token expired" in msg:
                print("[WARNING] Spotify token expired. Refresh needed.")
            else:
                print(f"[ERROR] {e}")

        time.sleep(1)


def cancel_future(fut):
    if fut is not None and not fut.done():
        fut.cancel()

