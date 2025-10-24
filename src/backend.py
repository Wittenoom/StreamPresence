import os
import time
import statistics
import provider as provider
import LRCHelpers as LRCHelpers
from EventSystem import ServerEvent
from multiprocessing import Process, Event, Queue
from queue import Empty
from sentence_transformers import SentenceTransformer

server = ServerEvent()


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

@server.on("stop")
def handle_pause(_):
    global_stop_event.set()
    print("Music Paused")

@server.on("preprocess")
def preprocessing(track_info):
    global lyric_process, presence_process
    global_stop_event.clear()
    item = track_info["item"]
    track_id = item["id"]
    name = item["name"]
    artist = item["artists"][0]["name"]
    album = item["album"]["name"]
    duration = item["duration_ms"]

    print(f"[SYNC] New song detected: {name} - {artist}")
    if track_id in lyrics_cache:
        parsed_lyrics = lyrics_cache[track_id]["parsed"]
    else:
        results = provider.get_lyrics(f"{name} - {artist}")

        scored_songs = []
        for song in results:
            score = (
                    encode_and_similar(name, song["name"], model) * 100 +
                    encode_and_similar(artist, song["artistName"], model) * 90 +
                    encode_and_similar(album, song["albumName"], model) * 80 -
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

    # Queue data
    data = {
        "parsed_lyrics": parsed_lyrics,
        "progress": min(track_info["progress_ms"], duration),
        "track_duration": duration,
    }

    # Start workers if not running
    if not lyric_process or not lyric_process.is_alive():
        lyric_process = Process(target=lyric_sync_worker, args=(sync_queue, global_stop_event))
        lyric_process.start()

    # Put initial data into queue
    sync_queue.put(data)


@server.on("update")
def update(track_info):
    global_stop_event.clear()

    data = {
        "progress": min(track_info["progress_ms"], track_info["item"]["duration_ms"]),
        "track_duration": track_info["item"]["duration_ms"]
    }
    if (lyric_process and lyric_process.is_alive()):
        sync_queue.put(data)


if __name__ == "__main__":
    cache_dir = os.path.join(os.getcwd(), "envs")
    os.makedirs(cache_dir, exist_ok=True)
    env_path = os.path.join(cache_dir, ".env")

    sync_queue = Queue()
    lyric_process = None

    lyrics_cache = {}

    global_stop_event = Event()

    try:
        global_stop_event.clear()
        model = SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2')

        print("Welcome back")

        server.start()
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.stop()
        global_stop_event.set()

        if lyric_process and lyric_process.is_alive():
            lyric_process.join(timeout=2)
            if lyric_process.is_alive():
                lyric_process.terminate()


        lyric_process = None
        presence_process = None
        exit(0)