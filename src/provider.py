import httpx
from src.exceptions import TrackNotFound, RequestFailed

DEFAULT_HOST = "https://lrclib.net"
DEFAULT_HEADERS = {
    "User-Agent": "lrcdl v0.2.11 (https://github.com/viown/lrcdl)"
}

def get_lyrics(keywords: str):
    params = {
        "q": keywords.strip(),
    }

    with httpx.Client() as client:
        response = client.get(f"{DEFAULT_HOST}/api/search", params=params, headers=DEFAULT_HEADERS)

    if response.status_code == 200:
        return response.json()
    elif response.status_code == 404:
        raise TrackNotFound()
    else:
        raise RequestFailed(response.text)
