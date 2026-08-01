import re
import requests


SPOTIFYDOWN_API = "https://api.spotifydown.com"
SPOTIFYDOWN_HEADERS = {
    "Origin": "https://spotifydown.com",
    "Referer": "https://spotifydown.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}


def detect_url_type(url: str) -> str:
    """
    Returns 'track', 'playlist', or 'album' based on Spotify URL pattern.
    Raises ValueError for unrecognized Spotify URL shapes.
    """
    if re.search(r"open\.spotify\.com/track/", url):
        return "track"
    if re.search(r"open\.spotify\.com/playlist/", url):
        return "playlist"
    if re.search(r"open\.spotify\.com/album/", url):
        return "album"
    raise ValueError(f"Unrecognized Spotify URL format: {url}")


def extract_id(url: str) -> str:
    """Extracts the Spotify ID from a track/playlist/album URL."""
    match = re.search(r"spotify\.com/(?:track|playlist|album)/([A-Za-z0-9]+)", url)
    if not match:
        raise ValueError(f"Could not extract Spotify ID from URL: {url}")
    return match.group(1)


def get_track_metadata(track_id: str) -> dict:
    """
    Fetches track metadata from SpotifyDown API.
    Returns dict with: id, title, artists, album, cover, isrc, etc.
    Raises RuntimeError on failure.
    """
    resp = requests.get(
        f"{SPOTIFYDOWN_API}/metadata/track/{track_id}",
        headers=SPOTIFYDOWN_HEADERS,
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"SpotifyDown metadata failed: HTTP {resp.status_code}")
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"SpotifyDown metadata error: {data.get('message', 'unknown')}")
    return data


def download_track(track_id: str) -> str:
    """
    Fetches a direct MP3 download URL for a track via SpotifyDown API.
    Returns the download URL string.
    Raises RuntimeError on failure.
    """
    resp = requests.get(
        f"{SPOTIFYDOWN_API}/download/{track_id}",
        headers=SPOTIFYDOWN_HEADERS,
        timeout=20,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"SpotifyDown download failed: HTTP {resp.status_code}")
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"SpotifyDown error: {data.get('message', 'unknown')}")
    link = data.get("link", "")
    if not link:
        raise RuntimeError("SpotifyDown returned no download link")
    return link


def get_playlist_tracks(playlist_id: str) -> list[dict]:
    """
    Fetches all tracks from a Spotify playlist via SpotifyDown API.
    Returns list of track dicts with at minimum 'id' and 'title' fields.
    Handles pagination automatically.
    """
    tracks = []
    page = 0

    while True:
        resp = requests.get(
            f"{SPOTIFYDOWN_API}/trackList/playlist/{playlist_id}",
            headers=SPOTIFYDOWN_HEADERS,
            params={"offset": page * 100},
            timeout=20,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"SpotifyDown playlist fetch failed: HTTP {resp.status_code}")
        data = resp.json()
        if not data.get("success"):
            raise RuntimeError(f"SpotifyDown playlist error: {data.get('message', 'unknown')}")

        page_tracks = data.get("trackList", [])
        tracks.extend(page_tracks)

        if not data.get("nextOffset"):
            break
        page += 1

    return tracks


def get_album_tracks(album_id: str) -> list[dict]:
    """
    Fetches all tracks from a Spotify album via SpotifyDown API.
    Returns list of track dicts.
    """
    resp = requests.get(
        f"{SPOTIFYDOWN_API}/trackList/album/{album_id}",
        headers=SPOTIFYDOWN_HEADERS,
        timeout=20,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"SpotifyDown album fetch failed: HTTP {resp.status_code}")
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"SpotifyDown album error: {data.get('message', 'unknown')}")
    return data.get("trackList", [])


def safe_filename(title: str, artists: str) -> str:
    """Builds a filesystem-safe filename from track title and artists."""
    name = f"{artists} - {title}"
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = name.strip()
    return f"{name}.mp3"
