import os
import re
from pathlib import Path


def detect_url_type(url: str) -> str:
    """
    Returns 'track', 'playlist', or 'album' based on Spotify URL pattern.
    Raises ValueError for unrecognized Spotify URL shapes.
    """
    track_pattern = re.compile(r"https?://open\.spotify\.com/track/[A-Za-z0-9]+")
    playlist_pattern = re.compile(r"https?://open\.spotify\.com/playlist/[A-Za-z0-9]+")
    album_pattern = re.compile(r"https?://open\.spotify\.com/album/[A-Za-z0-9]+")

    if track_pattern.search(url):
        return "track"
    if playlist_pattern.search(url):
        return "playlist"
    if album_pattern.search(url):
        return "album"

    raise ValueError(f"Unrecognized Spotify URL format: {url}")


def build_spotdl_args(url: str, fmt: str, bitrate: str, output_dir: str, cookies_path: str = "") -> list[str]:
    """
    Returns a fully-formed list of CLI args for spotdl.

    fmt          — one of: mp3, flac, m4a, opus
    bitrate      — one of: 128k, 192k, 256k, 320k (ignored when fmt == 'flac')
    output_dir   — absolute path to the temp working directory for this job
    cookies_path — optional path to a Netscape cookies.txt file for yt-dlp
    """
    valid_formats = {"mp3", "flac", "m4a", "opus"}
    valid_bitrates = {"128k", "192k", "256k", "320k"}

    if fmt not in valid_formats:
        raise ValueError(f"Invalid format '{fmt}'. Must be one of: {valid_formats}")

    if fmt != "flac" and bitrate not in valid_bitrates:
        raise ValueError(f"Invalid bitrate '{bitrate}'. Must be one of: {valid_bitrates}")

    Path(output_dir).mkdir(parents=True, exist_ok=True)

    args = [
        "spotdl",
        "download",
        url,
        "--output", output_dir,
        "--format", fmt,
    ]

    # FLAC is lossless — bitrate flag is meaningless and spotdl will reject it
    if fmt != "flac":
        args.extend(["--bitrate", bitrate])

    # Pass cookies to yt-dlp to bypass YouTube bot detection on cloud IPs
    if cookies_path and Path(cookies_path).exists():
        args.extend(["--cookie-file", cookies_path])

    # Proxy is injected via HTTP_PROXY/HTTPS_PROXY env vars in the subprocess
    # call — spotdl's --proxy flag rejects authenticated proxy URLs.

    # Pass own Spotify credentials to avoid shared client rate limits
    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "").strip()
    if client_id and client_secret:
        args.extend(["--client-id", client_id, "--client-secret", client_secret])

    return args
