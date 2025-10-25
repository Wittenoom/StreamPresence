import os
import time
import uvicorn
import statistics
from threading import Thread
from multiprocessing import Process, Event, Queue
from dotenv import load_dotenv
from sentence_transformers import SentenceTransformer

import provider
import LRCHelpers
from frontend import app
from EventSystem import ServerEvent
from DiscordRPCWrapper import DiscordRPCWrapper
from helpers import lyric_sync_worker, presence_sync_worker, encode_and_similar


class StreamPresenceBackend:
    def __init__(self):
        # --- environment setup ---
        cache_dir = os.path.join(os.getcwd(), "envs")
        os.makedirs(cache_dir, exist_ok=True)
        env_path = os.path.join(cache_dir, ".env")
        load_dotenv(dotenv_path=env_path)

        # --- core components ---
        self.sync_queue = Queue()
        self.lyric_process = None
        self.presence_process = None
        self.lyrics_cache = {}
        self.stop_event = Event()

        self.rpc = DiscordRPCWrapper(os.getenv("DISCORD_APP_ID"))
        self.server = ServerEvent()
        self.model = SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2')

        self._bind_hooks()

    # ------------------------------------------------------------------------
    # Event Hook Bindings
    # ------------------------------------------------------------------------
    def _bind_hooks(self):
        @self.rpc.hook("on_ready")
        def _():
            print("RPC THREAD IS READY", flush=True)

        @self.rpc.hook("on_error")
        def _(e):
            print(f"[RPC ERROR] {e}")

        @self.server.on("stop")
        def _(_data):
            self.handle_pause()

        @self.server.on("preprocess")
        def _(track_info):
            self.preprocessing(track_info)

        @self.server.on("update")
        def _(track_info):
            self.update(track_info)

    # ------------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------------
    def handle_pause(self):
        self.stop_event.set()
        print("Music Paused")
        self.rpc.update_presence(
            details="⏸️ Nothing playing",
            large_image="spotify_app_logo_svg",
            start=int(time.time())
        )

    def preprocessing(self, track_info):
        self.stop_event.clear()
        item = track_info["item"]
        track_id = item["id"]
        name = item["name"]
        artist = item["artists"][0]["name"]
        album = item["album"]["name"]
        duration = item["duration_ms"]

        print(f"[SYNC] New song detected: {name} - {artist}")

        self.rpc.update_presence(
            details="Now playing",
            state=f"{name} - {artist}"[:255],
            large_image="spotify_app_logo_svg",
            large_text=album,
            start=int(time.time())
        )

        parsed_lyrics = self._fetch_or_cache_lyrics(track_id, name, artist, album, duration)
        if parsed_lyrics:
            data = {
                "parsed_lyrics": parsed_lyrics,
                "progress": min(track_info["progress_ms"], duration),
                "track_duration": duration,
                "album_name": album
            }

            # Start worker threads if not running
            if not self._workers_running():
                self.lyric_process = Process(target=lyric_sync_worker, args=(self.sync_queue, self.stop_event))
                self.presence_process = Thread(target=presence_sync_worker, args=(self.sync_queue, self.stop_event, self.rpc))
                self.presence_process.start()
                self.lyric_process.start()

            self.sync_queue.put(data)

    def update(self, track_info):
        self.stop_event.clear()
        item = track_info["item"]
        track_id = item["id"]

        cached = self.lyrics_cache.get(track_id)
        if not cached or not cached.get("parsed"):
            return

        data = {
            "parsed_lyrics": cached["parsed"],
            "progress": min(track_info["progress_ms"], item["duration_ms"]),
            "track_duration": item["duration_ms"],
            "album_name": item["album"]["name"]
        }

        if self._workers_running():
            self.sync_queue.put(data)

    # ------------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------------
    def _workers_running(self) -> bool:
        """Check if either lyric or presence worker is alive."""
        return (
            (self.lyric_process and self.lyric_process.is_alive()) or
            (self.presence_process and self.presence_process.is_alive())
        )

    def _fetch_or_cache_lyrics(self, track_id, name, artist, album, duration):
        """Return parsed lyrics, fetching from LRCLIB if not cached."""
        if track_id in self.lyrics_cache:
            return self.lyrics_cache[track_id]["parsed"]

        results = provider.get_lyrics(f"{name} - {artist}")
        scored = []

        for song in results:
            score = (
                encode_and_similar(name, song["name"], self.model) * 100 +
                encode_and_similar(artist, song["artistName"], self.model) * 90 +
                encode_and_similar(album, song["albumName"], self.model) * 80 -
                abs(duration // 1000 - song["duration"]) * 15
            )
            scored.append({
                "score": score,
                "trackname": song["trackName"],
                "lyrics": song.get("syncedLyrics")
            })

        scored.sort(key=lambda s: s["score"], reverse=True)
        q1, q2, q3 = statistics.quantiles([s["score"] for s in scored], n=4, method="inclusive")

        for s in scored:
            if s["score"] >= q3 and s.get("lyrics"):
                print(f"[DEBUG] Found lyrics for: {s['trackname']} (score: {s['score']:.2f})")
                parsed = LRCHelpers.parse_lrc(s["lyrics"])
                self.lyrics_cache[track_id] = {
                    "parsed": parsed,
                    "raw": s["lyrics"],
                    "timestamp": time.time()
                }
                return parsed
        return None

    # ------------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------------
    def run(self):
        """Main lifecycle entrypoint."""
        config = uvicorn.Config(
            app=app,
            host="127.0.0.1",
            port=8080,
            log_level="info",
        )
        frontend_server = uvicorn.Server(config)
        frontend_thread = Thread(target=frontend_server.run, daemon=True)

        print("Welcome back")
        frontend_thread.start()
        self.server.start()
        self.rpc.start()
        self.rpc.update_presence(
            details="⏸️ Nothing playing",
            large_image="spotify_app_logo_svg",
            start=int(time.time())
        )

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop(frontend_server, frontend_thread)

    def stop(self, frontend_server, frontend_thread):
        """Gracefully shut down all services."""
        print("\n[EXIT] Cleaning up...")
        frontend_server.should_exit = True

        if frontend_thread and frontend_thread.is_alive():
            frontend_thread.join()

        self.server.stop()
        self.rpc.stop()
        self.stop_event.set()

        if self.lyric_process and self.lyric_process.is_alive():
            self.lyric_process.join(timeout=2)
            if self.lyric_process.is_alive():
                self.lyric_process.terminate()

        self.lyric_process = None
        self.presence_process = None

        print("[EXIT] All services stopped cleanly.")
        exit(0)


# ------------------------------------------------------------------------
# Entrypoint
# ------------------------------------------------------------------------
if __name__ == "__main__":
    backend = StreamPresenceBackend()
    backend.run()
