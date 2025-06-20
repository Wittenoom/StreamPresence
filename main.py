import os
import time
from DiscordRPCWrapper import DiscordRPCWrapper
import asyncio
import provider
import LRCHelpers
from sentence_transformers import SentenceTransformer
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
from ctypes import wintypes
from threading import Lock, Event
from quart import Quart, redirect, request, url_for
from spotipy import Spotify, SpotifyOAuth
load_dotenv()

# Spotify API Credentials
CLIENT_ID = os.getenv("SPOTIPY_CLIENT_ID")
CLIENT_SECRET = os.getenv("SPOTIPY_CLIENT_SECRET")
REDIRECT_URI = "http://127.0.0.1:8888/callback"
scope="user-read-playback-state user-read-currently-playing playlist-read-private"

# === GLOBAL STATE ==
rpc = DiscordRPCWrapper(os.getenv("DISCORD_APP_ID"))
app = Quart(__name__)

oauth = SpotifyOAuth(
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET,
    redirect_uri=REDIRECT_URI,
    scope=scope,
)

token_lock = Lock()
stop_event = Event()
executor = ThreadPoolExecutor(max_workers=20)  # Only 10 polling thread

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
    else:
        return None


# === START/STOP HOOKS ===

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

# === ROUTES ===

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

# === RPC HOOKS ===

@rpc.hook("on_ready")
def on_ready():
    print("RPC THREAD IS READY", flush=True)


@rpc.hook("on_error")
def error(e):
    print(e, flush=True)


# === POLLING WORKER ===



def polling_worker():
    last_track_id = None
    last_lyric_timestamp = None
    lrc = None

    while not stop_event.is_set():
        sp = get_spotify_client()

        try:
            track = sp.current_user_playing_track()
            if track and track["is_playing"]:
                track_item = track["item"]
                track_id = track_item["id"]

                if track_id != last_track_id:
                    lrc = None
                    counter = 0
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
                        name1 = song["name"]
                        trackName = song["trackName"]
                        artistName = song["artistName"]
                        albumName = song["albumName"]
                        duration1 = song["duration"]
                        namesimilarity = encode_and_similar(name, name1, model)
                        artistsimilarity = encode_and_similar(artist_name, artistName, model)
                        albumsimilarity = encode_and_similar(album_name, albumName, model)

                        duration_penalty = abs(duration - duration1)
                        score = (namesimilarity[0] * 100) + (
                                    artistsimilarity[0] * 90) + (albumsimilarity[0] * 80) - (
                                            duration_penalty * 15)

                        songdatas.append({"score": score, "songid": song["id"], "trackname": trackName,
                                          "lyrics": song.get("syncedLyrics")})

                    last_track_id = track_id

                progress = track["progress_ms"]


                if songdatas:
                    sorted_songs = sorted(songdatas, key=lambda s: s["score"], reverse=True)
                    # Calculate average score
                    average_score = sum(song["score"] for song in sorted_songs) / len(sorted_songs)


                    if not lrc:
                        for songdata in sorted_songs:
                            score = songdata["score"]
                            if score < average_score:
                                lrc = None
                                print(f"[DEBUG] Skipping below-average score: {score:.2f} < {average_score:.2f}")
                                continue

                            lrc = songdata.get("lyrics")
                            if lrc:
                                print(f"[DEBUG] Found lyrics for: {songdata['trackname']} (score: {score:.2f})")
                                break

                    if isinstance(lrc, str):
                        parsed_lyrics = LRCHelpers.parse_lrc(lrc)
                        current_lyric = LRCHelpers.get_synced_line(parsed_lyrics, progress)

                        if current_lyric:
                            timestamp = current_lyric["time"]
                            current_line = current_lyric["text"]

                            if timestamp != last_lyric_timestamp:
                                state = current_line if current_line else "🎵"
                                now = time.time()
                                start_timestamp = int(now - progress // 1000)
                                end_timestamp = int(start_timestamp + duration)

                                rpc.update_presence(
                                    state=state,
                                    details=f"{name} - {artist_name} [{album_name}]",
                                    start=start_timestamp,
                                    end=end_timestamp
                                )

                                print(f"🎤 {state}", flush=True)
                                last_lyric_timestamp = timestamp
                                counter += 1


            elif last_track_id is not None:
                    now = time.time()
                    start_timestamp = int(now - progress//1000)
                    end_timestamp = int(start_timestamp + duration)

                    rpc.update_presence(
                        details=f"Nothing Playing",
                        start=start_timestamp,
                        end=end_timestamp
                        )
                    print("Nothing playing", flush=True)

                    last_lyric_line = None
                    last_track_id = None

        except Exception as e:
            if "access token expired" in str(e).lower():
                print("[WARNING] Spotify access token expired. Attempting to refresh...", flush=True)
            else:
                print(f"[ERROR] {e}", flush=True)

    time.sleep(1/45)



@app.after_serving
async def shutdown():
    global POLLER_FUTURE, RPC_FUTURE, MODEL
    rpc.stop()
    stop_event.set()

    # Cancel refresher if it exists


    # Wait for poller to finish
    if POLLER_FUTURE:
        await asyncio.wrap_future(POLLER_FUTURE)

    if RPC_FUTURE:
        await asyncio.wrap_future(RPC_FUTURE)

    executor.shutdown(wait=True)