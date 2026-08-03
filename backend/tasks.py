import os
import re
import shutil
import ssl
import subprocess
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import redis as redis_lib
from celery import Celery

from downloader import build_spotdl_args, detect_url_type
from spotify_api import get_album_track_urls, get_playlist_track_urls
from storage import generate_presigned_url, upload_file

REDIS_URL = os.environ["REDIS_URL"]
JOB_TTL_SECONDS = 7200  # 2 hours
MAX_TRACKS = 50

celery_app = Celery("spotify_downloader", broker=REDIS_URL, backend=REDIS_URL)

_ssl_config = {"ssl_cert_reqs": ssl.CERT_NONE}

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    broker_connection_retry_on_startup=True,
    broker_use_ssl=_ssl_config,
    redis_backend_use_ssl=_ssl_config,
)

_COOKIES_PATH = "/tmp/yt_cookies.txt"
_SPOTDL_DEFAULT_CONFIG = "/root/.spotdl/config.json"


def _write_cookies_if_needed() -> None:
    b64 = os.environ.get("YOUTUBE_COOKIES_B64", "").strip()
    if not b64 or os.path.exists(_COOKIES_PATH):
        return
    import base64
    try:
        with open(_COOKIES_PATH, "wb") as f:
            f.write(base64.b64decode(b64))
    except Exception as exc:
        print(f"[warn] Failed to write cookies: {exc}")


def _write_spotdl_config_if_needed() -> None:
    if os.path.exists(_SPOTDL_DEFAULT_CONFIG):
        return
    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        return
    import json
    Path(_SPOTDL_DEFAULT_CONFIG).parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(_SPOTDL_DEFAULT_CONFIG, "w") as f:
            json.dump({"client_id": client_id, "client_secret": client_secret,
                       "auth_token": "", "use_cache_file": False, "no_cache": False}, f)
    except Exception as exc:
        print(f"[warn] Failed to write spotdl config: {exc}")


def _redis() -> redis_lib.Redis:
    return redis_lib.Redis.from_url(REDIS_URL, decode_responses=True, ssl_cert_reqs=ssl.CERT_NONE)


def _set_job_fields(r: redis_lib.Redis, job_id: str, **fields) -> None:
    r.hset(f"job:{job_id}", mapping={k: str(v) for k, v in fields.items()})
    r.expire(f"job:{job_id}", JOB_TTL_SECONDS)


def _download_single_track(track_url: str, fmt: str, bitrate: str,
                            output_dir: str, subprocess_env: dict) -> Path | None:
    """
    Downloads a single track via spotdl.
    Returns the Path to the downloaded file, or None on failure.
    """
    args = build_spotdl_args(track_url, fmt, bitrate, output_dir,
                             cookies_path=_COOKIES_PATH)
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=120, env=subprocess_env,
        )
        audio_extensions = {".mp3", ".flac", ".m4a", ".opus", ".ogg"}
        candidates = [
            p for p in Path(output_dir).iterdir()
            if p.is_file() and p.suffix.lower() in audio_extensions
        ]
        return candidates[0] if candidates else None
    except Exception:
        return None


@celery_app.task(bind=True, name="tasks.run_download", max_retries=0)
def run_download(self, job_id: str, url: str, fmt: str, bitrate: str) -> None:
    r = _redis()
    tmp_dir = f"/tmp/{job_id}"

    _set_job_fields(r, job_id, status="processing", progress=0,
                    created_at=datetime.now(tz=timezone.utc).isoformat())

    try:
        Path(tmp_dir).mkdir(parents=True, exist_ok=True)
        _write_cookies_if_needed()
        _write_spotdl_config_if_needed()

        url_type = detect_url_type(url)

        subprocess_env = os.environ.copy()
        proxy_url = os.environ.get("PROXY_URL", "").strip()
        if proxy_url:
            subprocess_env["HTTP_PROXY"] = proxy_url
            subprocess_env["HTTPS_PROXY"] = proxy_url
            subprocess_env["http_proxy"] = proxy_url
            subprocess_env["https_proxy"] = proxy_url

        _set_job_fields(r, job_id, progress=5)

        if url_type == "track":
            # ── Single track — direct spotdl download ──────────────────────
            cli_args = build_spotdl_args(url, fmt, bitrate, tmp_dir,
                                         cookies_path=_COOKIES_PATH)
            _set_job_fields(r, job_id, progress=10)

            result = subprocess.run(
                cli_args, capture_output=True, text=True,
                timeout=300, env=subprocess_env,
            )

            if result.returncode != 0:
                audio_extensions = {".mp3", ".flac", ".m4a", ".opus", ".ogg"}
                candidates = [p for p in Path(tmp_dir).iterdir()
                              if p.is_file() and p.suffix.lower() in audio_extensions]
                if not candidates:
                    stderr = (result.stderr or "").strip()[-800:]
                    stdout = (result.stdout or "").strip()[-400:]
                    raise RuntimeError(
                        f"spotdl exited with code {result.returncode}. "
                        f"stderr: {stderr} stdout: {stdout}"
                    )

            _set_job_fields(r, job_id, progress=70)

            audio_extensions = {".mp3", ".flac", ".m4a", ".opus", ".ogg"}
            candidates = [p for p in Path(tmp_dir).iterdir()
                          if p.is_file() and p.suffix.lower() in audio_extensions]
            if not candidates:
                raise RuntimeError(f"spotdl produced no audio file. stdout: {result.stdout[-400:]}")

            local_upload_path = str(candidates[0])
            filename = candidates[0].name

        else:
            # ── Playlist / Album — fetch track list via Spotify API,
            #    download each track individually in parallel ─────────────
            _set_job_fields(r, job_id, progress=8)

            # Extract Spotify ID
            id_match = re.search(r"/(playlist|album)/([A-Za-z0-9]+)", url)
            if not id_match:
                raise RuntimeError(f"Could not extract Spotify ID from URL: {url}")
            spotify_id = id_match.group(2)

            # Fetch track list
            if url_type == "playlist":
                track_urls = get_playlist_track_urls(spotify_id)
            else:
                track_urls = get_album_track_urls(spotify_id)

            if not track_urls:
                raise RuntimeError("No tracks found in playlist/album")

            total = len(track_urls)

            # Enforce 50-track limit
            if total > MAX_TRACKS:
                _set_job_fields(
                    r, job_id, status="error",
                    error=f"This playlist has {total} tracks. Maximum allowed is {MAX_TRACKS}. "
                          f"Please use a shorter playlist or download individual tracks.",
                )
                return

            _set_job_fields(r, job_id, progress=15)

            # Download each track in its own subdirectory to avoid filename collisions
            downloaded_paths: list[Path] = []
            completed = 0

            with ThreadPoolExecutor(max_workers=4) as pool:
                future_map = {}
                for i, track_url in enumerate(track_urls):
                    track_dir = f"{tmp_dir}/track_{i}"
                    Path(track_dir).mkdir(parents=True, exist_ok=True)
                    future = pool.submit(
                        _download_single_track,
                        track_url, fmt, bitrate, track_dir, subprocess_env,
                    )
                    future_map[future] = i

                for future in as_completed(future_map):
                    result_path = future.result()
                    if result_path:
                        downloaded_paths.append(result_path)
                    completed += 1
                    progress = 15 + int((completed / total) * 55)
                    _set_job_fields(r, job_id, progress=progress)

            if not downloaded_paths:
                raise RuntimeError("All tracks failed to download — no audio files produced")

            _set_job_fields(r, job_id, progress=72)

            # Zip all downloaded files
            zip_path = f"/tmp/{job_id}.zip"
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for p in downloaded_paths:
                    zf.write(str(p), arcname=p.name)

            local_upload_path = zip_path
            filename = f"{job_id}.zip"

        # ── Upload to R2 ───────────────────────────────────────────────────
        _set_job_fields(r, job_id, progress=80)
        object_key = f"{job_id}/{filename}"
        upload_file(local_upload_path, object_key)

        _set_job_fields(r, job_id, progress=92)
        presigned_url = generate_presigned_url(object_key, expiry_seconds=7200)

        _set_job_fields(r, job_id,
                        status="done", progress=100,
                        filename=filename,
                        download_url=presigned_url,
                        object_key=object_key)

    except subprocess.TimeoutExpired:
        _set_job_fields(r, job_id, status="error",
                        error="Download timed out. Try a shorter playlist or a single track.")
    except Exception as exc:
        _set_job_fields(r, job_id, status="error", error=str(exc)[:500])
    finally:
        if Path(tmp_dir).exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        zip_candidate = Path(f"/tmp/{job_id}.zip")
        if zip_candidate.exists():
            zip_candidate.unlink(missing_ok=True)
