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
import ctypes
import random
import win32gui
import win32api
import win32con

load_dotenv()

# Spotify API Credentials
CLIENT_ID = os.getenv("SPOTIPY_CLIENT_ID")
CLIENT_SECRET = os.getenv("SPOTIPY_CLIENT_SECRET")
REDIRECT_URI = "http://127.0.0.1:8888/callback"
scope = "user-read-playback-state user-read-currently-playing playlist-read-private"

# === GLOBAL STATE ===
sync_future = None
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

def get_spotify_client():
    with token_lock:
        token_info = oauth.get_cached_token()
        if not token_info:
            return None

        if oauth.is_token_expired(token_info):
            print("[INFO] Token expired. Refreshing...")
            token_info = oauth.refresh_access_token(token_info['refresh_token'])

        return Spotify(auth_manager=oauth)

def show_box(title, message):
    ctypes.windll.user32.MessageBoxW(0, message, title, 0x40 | 0x1)

def get_screen_bounds():
    pt = win32api.GetCursorPos()
    mon = win32api.MonitorFromPoint(pt, win32con.MONITOR_DEFAULTTONEAREST)
    info = win32api.GetMonitorInfo(mon)
    left, top, right, bottom = info['Monitor']
    return left, top, right - left, bottom - top

def move_box(title, stop_event: Event):
    left, top, width, height = get_screen_bounds()

    hwnd = None
    while hwnd is None:
        # Try to find the MessageBox window by its title
        def enum_handler(h, _):
            nonlocal hwnd
            if win32gui.IsWindowVisible(h) and win32gui.GetWindowText(h) == title:
                hwnd = h

        win32gui.EnumWindows(enum_handler, None)
        if hwnd is None:
            time.sleep(0.05)

    # Move loop
    while not stop_event.is_set():
        rect = win32gui.GetWindowRect(hwnd)
        w = rect[2] - rect[0]
        h = rect[3] - rect[1]
        x = random.randint(left, left + width - w)
        y = random.randint(top, top + height - h)
        win32gui.MoveWindow(hwnd, x, y, w, h, True)
        time.sleep(0.5)

    # Close the MessageBox
    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)

def encode_and_similar(arg1, arg2, MODEL):
    if MODEL:
        embeddings = MODEL.encode(arg1)
        embeddings1 = MODEL.encode(arg2)
        similarities = MODEL.similarity(embeddings, embeddings1)
        return similarities.tolist()[0]
    else:
        return None


def is_normal_window(hwnd):
    if not win32gui.IsWindowVisible(hwnd):
        return False
    if not win32gui.IsWindowEnabled(hwnd):
        return False
    if win32gui.GetParent(hwnd) != 0:
        return False
    style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
    if style & win32con.WS_EX_TOOLWINDOW:
        return False  # skip tool windows like Rainmeter
    return True

def minimize_normal_windows():
    def enum_handler(hwnd, _):
        if is_normal_window(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)

    win32gui.EnumWindows(enum_handler, None)

def lyric_sync_worker(fun_variable, sync_queue: Queue, sync_stop_event: Event):
    stop_dance_event = Event()
    drop_started = False  # top of lyric_sync_worker
    print("[SYNC] Worker thread started")
    parsed_lyrics = None
    start_time = None  # time.monotonic() is also an option if you want more precision
    track_duration = None
    last_timestamp = None

    while not sync_stop_event.is_set():
        try:
            new_data = sync_queue.get(timeout=1)
            parsed_lyrics = new_data["parsed_lyrics"]
            track_duration = new_data["track_duration"]  # in milliseconds
            progress = new_data["progress"]  # in milliseconds
            start_time = time.time() * 1000 - progress  # now in milliseconds
            print(f"[SYNC] Got new lyrics for {track_duration}ms with initial progress {progress}ms")
        except Empty:
            pass

        if not parsed_lyrics or start_time is None:
            time.sleep(0.1)
            continue

        elapsed = time.time() * 1000 - start_time  # in milliseconds
        if elapsed >= track_duration:
            print("[SYNC] Done: Song ended")
            break

        current_lyric = LRCHelpers.get_synced_line(parsed_lyrics, int(elapsed))
        if current_lyric and current_lyric["time"] != last_timestamp:
            last_timestamp = current_lyric["time"]
            state = current_lyric["text"] or "🎵"

            if not current_lyric["text"] and fun_variable:
                state = "!!! BANGER INCOMING !!!"
                minimize_normal_windows()
                if not drop_started:
                    drop_started = True
                    title = "DROP INCOMING"
                    title1 = "!DROP INCOMING!"
                    executor.submit(show_box, title, "!!! BANGER INCOMING !!!")
                    executor.submit(show_box, title1, "!!! BANGER INCOMING !!!")
                    executor.submit(move_box, title, stop_dance_event)
                    executor.submit(move_box, title1, stop_dance_event)

            elif current_lyric["text"] and drop_started:
                stop_dance_event.set()
                stop_dance_event = Event()  # reset for next round
                drop_started = False  # reset so next drop can trigger again

            print(f"🎤 {state}", flush=True)

            rpc.update_presence(
                state=state,
                start=int((start_time) // 1000),
                end=int((start_time + track_duration) // 1000),
                details="🎵 Lyrics"
            )



@app.before_serving
async def startup():
    global RPC_FUTURE, POLLER_FUTURE, MODEL
    rpc._running.set()
    loop = asyncio.get_running_loop()

    MODEL = await loop.run_in_executor(executor, SentenceTransformer, 'paraphrase-multilingual-MiniLM-L12-v2')
    POLLER_FUTURE = loop.run_in_executor(executor, polling_worker)
    RPC_FUTURE = loop.run_in_executor(rpc._executor, rpc._run)

    rpc.update_presence(details="⏸️ Nothing playing", start=int(time.time()))
    print("[INFO] BG tasks started")

@app.after_serving
async def shutdown():
    global POLLER_FUTURE, RPC_FUTURE
    rpc.stop()
    stop_event.set()

    if sync_future and not sync_future.done():
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

@rpc.hook("on_ready")
def on_ready():
    print("RPC THREAD IS READY", flush=True)

@rpc.hook("on_error")
def error(e):
    print(e, flush=True)

def polling_worker():
    global sync_future, sync_stop_event
    last_track_id = None
    progress = 0
    duration = 0
    fun_value = False

    while not stop_event.is_set():
        sp = get_spotify_client()

        try:
            track = sp.current_user_playing_track()
            if track and track["is_playing"]:
                track_item = track["item"]
                track_id = track_item["id"]

                if track_id != last_track_id:
                    lrc = None
                    name = track_item["name"]
                    artist_name = track_item["artists"][0]["name"]
                    album_name = track_item["album"]["name"]
                    duration = int(track_item["duration_ms"] / 1000)
                    progress = int(track["progress_ms"] / 1000)

                    print(f"🎵 Now Playing: {name} - {artist_name} [{album_name}]")

                    now = time.time()
                    start_timestamp = int(now - progress)
                    end_timestamp = int(start_timestamp + duration)

                    rpc.update_presence(
                        state="🎵",
                        details=f"{name} - {artist_name} [{album_name}]",
                        start=start_timestamp,
                        end=end_timestamp
                    )

                    results = provider.get_lyrics(f"{name} - {artist_name}")
                    model = MODEL
                    songdatas = []

                    for song in results:
                        score = (
                            encode_and_similar(name, song["name"], model)[0] * 100 +
                            encode_and_similar(artist_name, song["artistName"], model)[0] * 90 +
                            encode_and_similar(album_name, song["albumName"], model)[0] * 80 -
                            abs(duration - song["duration"]) * 15
                        )
                        songdatas.append({
                            "score": score,
                            "songid": song["id"],
                            "trackname": song["trackName"],
                            "lyrics": song.get("syncedLyrics")
                        })

                    last_track_id = track_id

                    sorted_songs = sorted(songdatas, key=lambda s: s["score"], reverse=True)
                    average_score = sum(song["score"] for song in sorted_songs) / len(sorted_songs)
                    if track_id == "6xaNsFM23AUFbtSCclfjKB":
                        print("[WARN] The dev loves this song, Be prepared for some antics")
                        rpc.update_presence(
                            state="The dev loves this song",
                            details=f"{name} - {artist_name} [{album_name}]",
                        )
                        fun_value = True


                    for songdata in sorted_songs:
                        if songdata["score"] < average_score:
                            print(f"[DEBUG] Skipping below-average score: {songdata['score']:.2f} < {average_score:.2f}")
                            continue
                        lrc = songdata.get("lyrics")
                        if lrc:
                            print(f"[DEBUG] Found lyrics for: {songdata['trackname']} (score: {songdata['score']:.2f})")
                            break

                    if isinstance(lrc, str):
                        parsed_lyrics = LRCHelpers.parse_lrc(lrc)

                        if sync_future and not sync_future.done():
                            sync_stop_event.set()
                            sync_future.cancel()

                        # Recalculate progress right before dispatch
                        elapsed_ms = int(
                            (time.time() - now) * 1000)  # how much time has passed since track info was fetched
                        adjusted_progress = min(track["progress_ms"] + elapsed_ms + 85, track_item["duration_ms"])

                        sync_data = {
                            "parsed_lyrics": parsed_lyrics,
                            "progress": adjusted_progress,  # in millis, more accurate
                            "track_duration": track_item["duration_ms"]
                        }

                        sync_queue.put(sync_data)

                        # Always push new data to the queue
                        sync_queue.put(sync_data)

                        if sync_future is None or sync_future.done():
                            sync_stop_event = Event()
                            sync_future = executor.submit(lyric_sync_worker, fun_value, sync_queue, sync_stop_event)


            elif last_track_id is not None:
                if sync_future and not sync_future.done():
                    sync_stop_event.set()
                    sync_future.cancel()


                for _ in range(5):
                    now = time.time()
                    start_timestamp = int(now - progress // 1000)
                    end_timestamp = int(start_timestamp + duration)
                    rpc.update_presence(
                        details="Nothing Playing",
                        start=start_timestamp,
                        end=end_timestamp
                    )

                print("Nothing playing", flush=True)
                last_track_id = None

        except Exception as e:
            if "access token expired" in str(e).lower():
                print("[WARNING] Spotify access token expired. Attempting to refresh...", flush=True)
            else:
                print(f"[ERROR] {e}", flush=True)

        time.sleep(0.5)
