import os
import time
from DiscordRPCWrapper import DiscordRPCWrapper
import asyncio
import provider
import LRCHelpers
from queue import Queue, Empty
from sentence_transformers import SentenceTransformer
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
from threading import Lock, Event
from quart import Quart, redirect, request, url_for
from spotipy import Spotify, SpotifyOAuth
import statistics
import tracemalloc

tracemalloc.start()
load_dotenv()

# Spotify API Credentials
CLIENT_ID = os.getenv("SPOTIPY_CLIENT_ID")
CLIENT_SECRET = os.getenv("SPOTIPY_CLIENT_SECRET")
REDIRECT_URI = "http://127.0.0.1:8888/callback"
scope = "user-read-playback-state user-read-currently-playing playlist-read-private"

# === GLOBAL STATE ===
lyrics_cache = {}
sync_future = None
presence_future = None
sync_stop_event = Event()

rpc = DiscordRPCWrapper(os.getenv("DISCORD_APP_ID"))
app = Quart(__name__)
sync_queue = Queue()

oauth = SpotifyOAuth(
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET,
    redirect_uri=REDIRECT_URI,
    scope=scope,
)

token_lock = Lock()
stop_event = Event()
executor = ThreadPoolExecutor(max_workers=20)


# --- Helper Functions ---
def get_spotify_client():
    with token_lock:
        token_info = oauth.get_cached_token()
        if not token_info:
            return None

        if oauth.is_token_expired(token_info):
            print("[INFO] Token expired. Refreshing...")
            token_info = oauth.refresh_access_token(token_info['refresh_token'])

        return Spotify(auth_manager=oauth)


def encode_and_similar(arg1, arg2, MODEL):
    if MODEL:
        embeddings = MODEL.encode(arg1)
        embeddings1 = MODEL.encode(arg2)
        similarities = MODEL.similarity(embeddings, embeddings1)
        return similarities.tolist()[0]
    return None


# --- Lyric Sync Worker ---
def lyric_sync_worker(sync_queue: Queue, sync_stop_event: Event):
    print("[SYNC] Worker thread started")
    parsed_lyrics = None
    track_duration = None
    album_name = None
    last_line_timestamp = None
    start_time_ns = 0

    while not sync_stop_event.is_set():
        try:
            new_data = sync_queue.get(timeout=0.05)
            parsed_lyrics = new_data["parsed_lyrics"]
            track_duration = new_data["track_duration"]
            progress_ms = new_data["progress"]
            album_name = new_data["album_name"]
            start_time_ns = time.perf_counter_ns() - progress_ms * 1_000_000

        except Empty:
            pass

        if not parsed_lyrics or start_time_ns is None:
            time.sleep(0.05)
            continue

        elapsed_ns = time.perf_counter_ns() - start_time_ns
        elapsed_ms = elapsed_ns // 1_000_000

        if elapsed_ms >= track_duration:
            print("[SYNC] Done: Song ended")
            sync_stop_event.set()
            break

        # Print lyric once per timestamp
        current_lyric = LRCHelpers.get_synced_line(parsed_lyrics, int(elapsed_ms))
        if current_lyric and current_lyric["time"] != last_line_timestamp:
            last_line_timestamp = current_lyric["time"]
            print(current_lyric["text"], flush=True)


def presence_sync_worker(sync_queue: Queue, sync_stop_event: Event):
    print("[SYNC] Worker thread started")
    parsed_lyrics = None
    track_duration = None
    album_name = None
    window_size_ms = 15000
    last_presence_update_ms = 0
    first_presence_update = True
    start_time_ns = 0
    last_line = None

    while not sync_stop_event.is_set():
        try:
            new_data = sync_queue.get(timeout=0.05)
            parsed_lyrics = new_data["parsed_lyrics"]
            track_duration = new_data["track_duration"]
            progress_ms = new_data["progress"]
            album_name = new_data["album_name"]
            start_time_ns = time.perf_counter_ns() - progress_ms * 1_000_000

        except Empty:
            pass

        if not parsed_lyrics or start_time_ns is None:
            time.sleep(0.05)
            continue

        elapsed_ns = time.perf_counter_ns() - start_time_ns
        elapsed_ms = elapsed_ns // 1_000_000

        if elapsed_ms >= track_duration:
            print("[SYNC] Done: Song ended")
            sync_stop_event.set()
            break

        # Update Discord presence every window
        if first_presence_update or (elapsed_ms - last_presence_update_ms >= window_size_ms):
            last_presence_update_ms = elapsed_ms
            first_presence_update = False
            window_end = elapsed_ms + window_size_ms

            future_lines = [
                str(line["text"]).replace("\n", "") or "🎵"
                for line in parsed_lyrics
                if elapsed_ms <= line["time"] <= window_end
            ]

            state = (f"{last_line}. " if last_line else "") + ". ".join(future_lines)
            if len(state) > 100:
                state = state[:100] + "..."
            last_line = future_lines[-1]
            rpc.update_presence(
                state=state or "🎵",
                start=int(start_time_ns // 1_000_000_000),
                end=int((start_time_ns // 1_000_000 + track_duration) // 1000),
                large_image="spotify_app_logo_svg",
                large_text=album_name,
                details="Lyrics"
            )


# --- Quart Lifecycle ---
@app.before_serving
async def startup():
    global RPC_FUTURE, POLLER_FUTURE, MODEL
    rpc._running.set()
    loop = asyncio.get_running_loop()

    MODEL = await loop.run_in_executor(executor, SentenceTransformer, 'paraphrase-multilingual-MiniLM-L12-v2')
    POLLER_FUTURE = loop.run_in_executor(executor, polling_worker)
    RPC_FUTURE = loop.run_in_executor(rpc._executor, rpc._run)

    rpc.update_presence(details="⏸️ Nothing playing", large_image="spotify_app_logo_svg", start=int(time.time()))
    print("[INFO] BG tasks started")


@app.after_serving
async def shutdown():
    global POLLER_FUTURE, RPC_FUTURE, sync_future, presence_future
    rpc.stop()
    stop_event.set()
    tracemalloc.stop()
    if presence_future and sync_future and not sync_future.done():
        sync_stop_event.set()
        sync_future.cancel()

    if POLLER_FUTURE:
        await asyncio.wrap_future(POLLER_FUTURE)
    if RPC_FUTURE:
        await asyncio.wrap_future(RPC_FUTURE)

    executor.shutdown(wait=True)


@app.route("/")
async def index():
    auth_url = oauth.get_authorize_url()
    return f'<a href="{auth_url}">Authorize with Spotify</a>'


@app.route("/callback")
async def callback():
    code = request.args.get("code")
    with token_lock:
        try:
            oauth.get_access_token(code)
        except Exception as e:
            return f"Failed to get token: {e}"

    return redirect(url_for("status"))


@app.route("/status")
async def status():
    sp = get_spotify_client()
    if not sp:
        return "No token available yet."

    user = sp.current_user()
    return f"Hello, {user['display_name']}!"


# --- Discord RPC Hooks ---
@rpc.hook("on_ready")
def on_ready():
    print("RPC THREAD IS READY", flush=True)


@rpc.hook("on_error")
def error(e):
    pass


# --- Spotify Polling Worker ---
def polling_worker():
    global presence_future, sync_future, sync_stop_event
    last_track_id = None
    parsed_lyrics = None


    while not stop_event.is_set():
        sp = get_spotify_client()
        try:
            track = sp.current_user_playing_track()

            if track and track["is_playing"]:
                track_item = track["item"]
                track_id = track_item["id"]
                name = track_item["name"]
                artist_name = track_item["artists"][0]["name"]
                album_name = track_item["album"]["name"]
                duration = track_item["duration_ms"]

                # New song
                if track_id != last_track_id:
                    print("[SYNC] Stopping lyric worker (Change song)")
                    if presence_future and sync_future and not sync_future.done():
                        sync_stop_event.set()
                        sync_future.cancel()
                        presence_future.cancel()


                    print(f"Now Playing: {name} - {artist_name} [{album_name}]")
                    rpc.update_presence(details="Now playing",
                                        state=f"{name} - {artist_name}"[:255],
                                        large_image="spotify_app_logo_svg",
                                        large_text=f"{album_name}",
                                        start=int(time.time()))
                    last_track_id = track_id

                    if track_id in lyrics_cache:
                        print(f"[CACHE] Using cached lyrics for {name} - {artist_name}")
                        parsed_lyrics = lyrics_cache[track_id]["parsed"]
                    else:
                        results = provider.get_lyrics(f"{name} - {artist_name}")
                        songdatas = []

                        for song in results:
                            score = (
                                    encode_and_similar(name, song["name"], MODEL)[0] * 100 +
                                    encode_and_similar(artist_name, song["artistName"], MODEL)[0] * 90 +
                                    encode_and_similar(album_name, song["albumName"], MODEL)[0] * 80 -
                                    abs(duration // 1000 - song["duration"]) * 15
                            )
                            songdatas.append({
                                "score": score,
                                "songid": song["id"],
                                "trackname": song["trackName"],
                                "lyrics": song.get("syncedLyrics")
                            })

                        sorted_songs = sorted(songdatas, key=lambda s: s["score"], reverse=True)
                        q1, q2, q3 = statistics.quantiles([s["score"] for s in sorted_songs], n=4, method="inclusive")
                        lrc = None
                        for songdata in sorted_songs:
                            if songdata["score"] < 0 or songdata["score"] < q3:
                                continue
                            lrc = songdata.get("lyrics")
                            if lrc:
                                print(
                                    f"[DEBUG] Found lyrics for: {songdata['trackname']} (score: {songdata['score']:.2f})")
                                break

                        parsed_lyrics = LRCHelpers.parse_lrc(lrc) if lrc else None
                        lyrics_cache[track_id] = {"parsed": parsed_lyrics, "raw": lrc, "timestamp": time.time()}

                # --- Mid-song resync ---
                if parsed_lyrics:
                    elapsed_ns = time.perf_counter_ns()
                    adjusted_progress = min(track["progress_ms"] + (elapsed_ns - elapsed_ns) // 1_000_000 + 175,
                                            duration)

                    sync_data = {
                        "parsed_lyrics": parsed_lyrics,
                        "progress": adjusted_progress,
                        "track_duration": duration,
                        "album_name": album_name
                    }
                    sync_queue.put(sync_data)

                    if sync_future is None or sync_future.done():
                        sync_stop_event = Event()
                        sync_future = executor.submit(lyric_sync_worker, sync_queue, sync_stop_event)
                        presence_future = executor.submit(presence_sync_worker, sync_queue, sync_stop_event)

            else:
                if last_track_id is not None:
                    print("[SYNC] Stopping lyric worker (track ended or paused)")
                    if presence_future and sync_future and not sync_future.done():
                        sync_stop_event.set()
                        sync_future.cancel()
                        presence_future.cancel()

                rpc.update_presence(details="⏸️ Nothing playing",
                                    large_image="spotify_app_logo_svg",
                                    start=int(time.time()))
                last_track_id = None
                parsed_lyrics = None

        except Exception as e:
            if "access token expired" in str(e).lower():
                print("[WARNING] Spotify access token expired. Attempting to refresh...")
            else:
                print(f"[ERROR] {e}")

        time.sleep(1)
