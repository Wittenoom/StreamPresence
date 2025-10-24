import os
import asyncio
from asyncio import Event
from dotenv import load_dotenv
from spotipy import SpotifyOAuth, Spotify
from quart import Quart, redirect, request, url_for
from EventSystem import ClientEvent

cache_dir = os.path.join(os.getcwd(), "envs")
os.makedirs(cache_dir, exist_ok=True)  # create the folder if it doesn't exist
cache_path = os.path.join(cache_dir, ".cache")
ws = ClientEvent("ws://localhost:8765")
env_path = os.path.join(cache_dir, ".env")
stop_event = Event()
load_dotenv(dotenv_path=env_path)

CLIENT_ID = os.getenv("SPOTIPY_CLIENT_ID")
CLIENT_SECRET = os.getenv("SPOTIPY_CLIENT_SECRET")
REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPE = "user-read-playback-state user-read-currently-playing"

app = Quart(__name__)
oauth = SpotifyOAuth(
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET,
    redirect_uri=REDIRECT_URI,
    scope=SCOPE,
    cache_path=cache_path
)


@app.route("/")
async def index():
    return f'<a href="{oauth.get_authorize_url()}">Authorize with Spotify</a>'


@app.route("/callback")
async def callback():
    code = request.args.get("code")
    await asyncio.to_thread(oauth.get_access_token, code)
    return redirect(url_for("status"))


@app.route("/status")
async def status():
    sp = Spotify(oauth_manager=oauth)
    user = await asyncio.to_thread(sp.current_user)
    return f"Hello, {user['display_name']}!"


@app.before_serving
async def startup():
    stop_event.clear()
    await ws.connect()
    app.add_background_task(poller_task)


@app.after_serving
async def stop():
    stop_event.set()
    await ws.close()


async def poller_task():
    last_track_id = None

    while not stop_event.is_set():
        sp = Spotify(oauth_manager=oauth)
        track_info = await asyncio.to_thread(sp.current_user_playing_track)

        if not track_info or not track_info.get("is_playing"):
            if last_track_id is not None:
                await ws.emit("stop")
                last_track_id = None

            await asyncio.sleep(1)
            continue

        item = track_info["item"]
        track_id = item["id"]

        if track_id != last_track_id:
            await ws.emit("preprocess", track_info)
            last_track_id = track_id

        if track_info and track_info.get("is_playing"):
            await ws.emit("update", track_info)

        await asyncio.sleep(2)