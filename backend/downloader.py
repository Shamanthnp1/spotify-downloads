import os
import re
from pathlib import Path


def detect_url_type(url: str) -> str:
    if re.search(r"open\.spotify\.com/track/", url):
        return "track"
    if re.search(r"open\.spotify\.com/playlist/", url):
        return "playlist"
    if re.search(r"open\.spotify\.com/album/", url):
        return "album"
    raise ValueError(f"Unrecognized Spotify URL format: {url}")


def build_spotdl_args(url: str, fmt: str, bitrate: str, output_dir: str, cookies_path: str = "") -> list[str]:
    """
    Returns a fully-formed list of CLI args for spotdl.
    """
    valid_formats = {"mp3", "flac", "m4a", "opus"}
    valid_bitrates = {"128k", "192k", "256k", "320k"}

    if fmt not in valid_formats:
        raise ValueError(f"Invalid format '{fmt}'. Must be one of: {valid_formats}")

    if fmt != "flac" and bitrate not in valid_bitrates:
        raise ValueError(f"Invalid bitrate '{bitrate}'. Must be one of: {valid_bitrates}")

    Path(output_dir).mkdir(parents=True, exist_ok=True)

    # Strip ?si= tracking params from URL
    clean_url = url.split("?")[0]

    args = [
        "spotdl",
        "download",
        clean_url,
        "--output", output_dir,
        "--format", fmt,
        "--audio", "youtube-music",
        "--audio", "youtube",
        "--ytm-data",
        "--preload",
    ]

    # FLAC is lossless — bitrate flag is meaningless
    if fmt != "flac":
        args.extend(["--bitrate", bitrate])

    # Pass cookies to yt-dlp
    if cookies_path and Path(cookies_path).exists():
        args.extend(["--cookie-file", cookies_path])

    # Pass Spotify credentials
    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "").strip()
    if client_id and client_secret:
        args.extend(["--client-id", client_id, "--client-secret", client_secret])

    return args
