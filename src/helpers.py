import time
from queue import Queue, Empty
from threading import Event
import LRCHelpers as LRCHelpers


# --- SentenceTransformer Helper ---
def encode_and_similar(arg1, arg2, model):
    """Compute similarity between two strings using a SentenceTransformer model."""
    if model:
        emb1 = model.encode(arg1)
        emb2 = model.encode(arg2)
        similarity = model.similarity(emb1, emb2)
        return similarity.tolist()[0][0]
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
            data = sync_queue.get(timeout=0.1)
        except Empty:
            data = None

        if data:
            if data.get("parsed_lyrics"):
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
            data = sync_queue.get(timeout=0.1)
        except Empty:
            data = None

        if data:
            if data.get("parsed_lyrics"):
                parsed_lyrics = data["parsed_lyrics"]

            if data.get("album_name"):
                album_name = data["album_name"]

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