"""
Spotify track list fetcher.
Uses spotdl's internal spotipyFree library to get playlist/album tracks
without requiring user OAuth — bypasses Spotify's Feb 2026 API restrictions.
"""
import os
import re
from typing import Optional


def _extract_spotify_id(url: str) -> tuple[str, str]:
    """Returns (url_type, spotify_id) from a Spotify URL."""
    match = re.search(r"open\.spotify\.com/(playlist|album|track)/([A-Za-z0-9]+)", url)
    if not match:
        raise ValueError(f"Could not extract Spotify ID from URL: {url}")
    return match.group(1), match.group(2)


def get_playlist_track_urls(playlist_id: str) -> list[str]:
    """
    Returns Spotify track URLs from a playlist using spotipyFree.
    Raises RuntimeError on failure.
    """
    try:
        from spotipyFree import Spotify as SpotifyFree
        sp = SpotifyFree()
        results = sp.playlist_tracks(playlist_id)
        urls = []
        items = results.get("items", [])
        while True:
            for item in items:
                track = item.get("track") if "track" in item else item
                if track and track.get("id") and not track.get("is_local"):
                    urls.append(f"https://open.spotify.com/track/{track['id']}")
            next_url = results.get("next")
            if not next_url:
                break
            results = sp._get(next_url)
            items = results.get("items", [])
        return urls
    except Exception as e:
        raise RuntimeError(f"Failed to fetch playlist tracks: {e}")


def get_album_track_urls(album_id: str) -> list[str]:
    """
    Returns Spotify track URLs from an album using spotipyFree.
    Raises RuntimeError on failure.
    """
    try:
        from spotipyFree import Spotify as SpotifyFree
        sp = SpotifyFree()
        results = sp.album_tracks(album_id)
        urls = []
        items = results.get("items", [])
        while True:
            for track in items:
                if track and track.get("id"):
                    urls.append(f"https://open.spotify.com/track/{track['id']}")
            next_url = results.get("next")
            if not next_url:
                break
            results = sp._get(next_url)
            items = results.get("items", [])
        return urls
    except Exception as e:
        raise RuntimeError(f"Failed to fetch album tracks: {e}")


def get_track_count(url_type: str, spotify_id: str) -> Optional[int]:
    """Returns track count for a playlist or album. Returns None on failure."""
    try:
        if url_type == "playlist":
            return len(get_playlist_track_urls(spotify_id))
        else:
            return len(get_album_track_urls(spotify_id))
    except Exception:
        return None

