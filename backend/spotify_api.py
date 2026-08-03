"""
Spotify API helpers — fetch playlist/album track lists using client credentials.
No user auth required. Works for public playlists and albums.
"""
import base64
import os
from typing import Optional

import requests


def _get_token() -> Optional[str]:
    """Returns a Spotify API bearer token using client credentials flow."""
    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        return None
    try:
        creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        resp = requests.post(
            "https://accounts.spotify.com/api/token",
            data={"grant_type": "client_credentials"},
            headers={"Authorization": f"Basic {creds}"},
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json().get("access_token")
    except Exception:
        pass
    return None


def get_playlist_track_urls(playlist_id: str) -> list[str]:
    """
    Returns a list of Spotify track URLs from a playlist.
    Handles pagination — returns all tracks.
    Raises RuntimeError on failure.
    """
    token = _get_token()
    if not token:
        raise RuntimeError("Could not get Spotify API token — check SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET")

    track_urls = []
    url = f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks"
    params = {"limit": 100, "offset": 0, "fields": "items(track(id,is_local)),next,total"}
    headers = {"Authorization": f"Bearer {token}"}

    while url:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        if resp.status_code != 200:
            raise RuntimeError(f"Spotify API error {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        for item in data.get("items", []):
            track = item.get("track")
            if track and not track.get("is_local") and track.get("id"):
                track_urls.append(f"https://open.spotify.com/track/{track['id']}")
        url = data.get("next")
        params = {}  # next URL already has params

    return track_urls


def get_album_track_urls(album_id: str) -> list[str]:
    """
    Returns a list of Spotify track URLs from an album.
    Handles pagination.
    Raises RuntimeError on failure.
    """
    token = _get_token()
    if not token:
        raise RuntimeError("Could not get Spotify API token")

    track_urls = []
    url = f"https://api.spotify.com/v1/albums/{album_id}/tracks"
    params = {"limit": 50, "offset": 0}
    headers = {"Authorization": f"Bearer {token}"}

    while url:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        if resp.status_code != 200:
            raise RuntimeError(f"Spotify API error {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        for track in data.get("items", []):
            if track and track.get("id"):
                track_urls.append(f"https://open.spotify.com/track/{track['id']}")
        url = data.get("next")
        params = {}

    return track_urls


def get_track_count(url_type: str, spotify_id: str) -> Optional[int]:
    """Returns track count for a playlist or album. Returns None on failure."""
    try:
        if url_type == "playlist":
            urls = get_playlist_track_urls(spotify_id)
        else:
            urls = get_album_track_urls(spotify_id)
        return len(urls)
    except Exception:
        return None
