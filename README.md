# Spotify Lyrics Discord RPC

This project creates a **Discord Rich Presence** integration for Spotify with **real-time synced lyrics**. It uses the Spotify Web API, **LRCLIB** for lyrics, and Discord's Rich Presence API to show the current song, album, artist, and lyric line on your Discord profile.

## Features

* Displays the current song, album, and artist info on Discord Rich Presence.
* Dynamically updates with synced lyrics from LRCLIB during playback.
* Uses `sentence_transformers` to select the most accurate lyric match based on metadata similarity.
* OAuth2 authentication flow to securely connect to your Spotify account.
* Local web server for managing authorization and checking status.

## Requirements

* Python 3.8+
* A Discord application with Rich Presence enabled
* A registered Spotify Developer application with the redirect URI:
  `http://127.0.0.1:8888/callback`

## Installation

1. Clone the repository:

```bash
git clone https://github.com/Wittenoom/StreamPresence.git
cd StreamPresence
```

2. Install the required packages:

```bash
pip install -r requirements.txt
```

3. Create a `.env` file in envs and configure it as follows:

```env
SPOTIPY_CLIENT_ID=your_spotify_client_id
SPOTIPY_CLIENT_SECRET=your_spotify_client_secret
DISCORD_APP_ID=your_discord_application_id
```

4. Start the application:

```bash
python src/main.py
```
Make sure to run the command in project root, Not in src directory

5. Open the printed URL in your browser and log in with your Spotify account to authorize the app.

## How It Works

* The `polling_worker` checks the currently playing Spotify track.
* On song change, it queries **LRCLIB** for lyrics using track name and artist.
* It ranks results using semantic similarity via **SentenceTransformer** to pick the most accurate match.
* Lyrics are synced to Spotify playback and updated in Discord Rich Presence.
* The `state` field in the RPC displays the current lyric line (if available).

## Tech Stack

* **Quart** – Async-compatible Flask-like web framework
* **Spotipy** – Lightweight client for Spotify Web API
* **LRCLIB** – Lyrics provider (synced and metadata-rich)
* **SentenceTransformer** – Semantic search and similarity ranking
* **DiscordRPCWrapper** – Custom integration for Discord Rich Presence
* **dotenv** – For managing secrets and environment configuration

## Known Limitations

* Requires Spotify Premium for full playback access.
* LRCLIB coverage may vary across songs.
* Semantic similarity model increases startup time due to model loading.



## License

MIT License
