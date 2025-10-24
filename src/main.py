import os
import time
import tracemalloc
from queue import Queue
from threading import Lock, Event
from concurrent.futures import ThreadPoolExecutor

import asyncio
from dotenv import load_dotenv
from quart import Quart, redirect, request, url_for
from spotipy import SpotifyOAuth
from sentence_transformers import SentenceTransformer

from src.DiscordRPCWrapper import DiscordRPCWrapper
from src.helpers import get_spotify_client, polling_worker, lyric_sync_worker, presence_sync_worker, cancel_future

# === INITIAL SETUP ===
tracemalloc.start()

cache_dir = os.path.join(os.getcwd(), "envs")
os.makedirs(cache_dir, exist_ok=True)  # create the folder if it doesn't exist
cache_path = os.path.join(cache_dir, ".cache")

env_path = os.path.join(cache_dir, ".env")
load_dotenv(dotenv_path=env_path)

CLIENT_ID = os.getenv("SPOTIPY_CLIENT_ID")
CLIENT_SECRET = os.getenv("SPOTIPY_CLIENT_SECRET")
REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPE = "user-read-playback-state user-read-currently-playing playlist-read-private"

lyrics_cache = {}
sync_future_ref = {"sync_future": None}
presence_future_ref = {"presence_future": None}
sync_queue = Queue()
sync_stop_event = Event()
stop_event = Event()
token_lock = Lock()
executor = ThreadPoolExecutor(max_workers=20)

rpc = DiscordRPCWrapper(os.getenv("DISCORD_APP_ID"))
app = Quart(__name__)

oauth = SpotifyOAuth(
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET,
    redirect_uri=REDIRECT_URI,
    scope=SCOPE,
    cache_path=cache_path
)

sync_future = None
presence_future = None

# === QUART LIFECYCLE ===
@app.before_serving
async def startup():
    global poller_future
    loop = asyncio.get_running_loop()
    model = await loop.run_in_executor(executor, SentenceTransformer, 'paraphrase-multilingual-MiniLM-L12-v2')

    poller_future = executor.submit(
        polling_worker,
        oauth, token_lock, rpc, lyrics_cache, sync_queue,
        executor, lyric_sync_worker, presence_sync_worker,
        model, stop_event,
        sync_future_ref, presence_future_ref,
        sync_stop_event
    )

    rpc.start()
    rpc.update_presence(details="⏸️ Nothing playing", large_image="spotify_app_logo_svg", start=int(time.time()))
    print("[INFO] Background tasks started")


@app.after_serving
async def shutdown():
    global poller_future, sync_future, presence_future
    rpc.stop()
    stop_event.set()
    tracemalloc.stop()
    sync_stop_event.set()

    cancel_future(sync_future)
    cancel_future(presence_future)

    if poller_future is not None:
        await asyncio.wrap_future(poller_future)
    executor.shutdown(wait=True)


# === ROUTES ===
@app.route("/")
async def index():
    return f'<a href="{oauth.get_authorize_url()}">Authorize with Spotify</a>'


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
    sp = get_spotify_client(oauth, token_lock)
    if not sp:
        return "No token available yet."
    user = sp.current_user()
    return f"Hello, {user['display_name']}!"


# === DISCORD RPC HOOKS ===
@rpc.hook("on_ready")
def on_ready():
    print("RPC THREAD IS READY", flush=True)


@rpc.hook("on_error")
def on_error(e):
    print(f"[RPC ERROR] {e}")