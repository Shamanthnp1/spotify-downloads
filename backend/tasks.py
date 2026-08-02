import os
import shutil
import ssl
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import redis as redis_lib
from celery import Celery

from downloader import build_spotdl_args, detect_url_type
from storage import delete_object, generate_presigned_url, upload_file

REDIS_URL = os.environ["REDIS_URL"]
JOB_TTL_SECONDS = 7200  # 2 hours

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


def _write_cookies_if_needed() -> None:
    """
    Decodes YOUTUBE_COOKIES_B64 env var and writes cookies.txt to disk once.
    No-op if the env var is not set or file already exists.
    """
    b64 = os.environ.get("YOUTUBE_COOKIES_B64", "").strip()
    if not b64:
        return
    if os.path.exists(_COOKIES_PATH):
        return
    import base64
    try:
        decoded = base64.b64decode(b64)
        with open(_COOKIES_PATH, "wb") as f:
            f.write(decoded)
    except Exception as exc:
        print(f"[warn] Failed to write cookies file: {exc}")


_SPOTDL_CONFIG_PATH = "/tmp/spotdl_config.json"
_SPOTDL_DEFAULT_CONFIG = "/root/.spotdl/config.json"


def _write_spotdl_config_if_needed() -> None:
    """
    Writes credentials to spotdl's default config location.
    Required after Feb 2026 Spotify API changes.
    """
    if os.path.exists(_SPOTDL_DEFAULT_CONFIG):
        return
    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        return
    import json
    Path(_SPOTDL_DEFAULT_CONFIG).parent.mkdir(parents=True, exist_ok=True)
    config = {
        "client_id": client_id,
        "client_secret": client_secret,
        "auth_token": "",
        "use_cache_file": False,
        "no_cache": False,
    }
    try:
        with open(_SPOTDL_DEFAULT_CONFIG, "w") as f:
            json.dump(config, f)
    except Exception as exc:
        print(f"[warn] Failed to write spotdl config: {exc}")


def _redis() -> redis_lib.Redis:
    return redis_lib.Redis.from_url(REDIS_URL, decode_responses=True, ssl_cert_reqs=ssl.CERT_NONE)


def _set_job_fields(r: redis_lib.Redis, job_id: str, **fields) -> None:
    key = f"job:{job_id}"
    r.hset(key, mapping={k: str(v) for k, v in fields.items()})
    r.expire(key, JOB_TTL_SECONDS)


def _zip_directory(source_dir: str, zip_path: str) -> None:
    """
    Zips all files in source_dir (non-recursively) into zip_path.
    Raises RuntimeError if no downloadable files are found.
    """
    files = [
        p for p in Path(source_dir).iterdir()
        if p.is_file() and not p.name.endswith(".spotdl")
    ]
    if not files:
        raise RuntimeError(f"spotdl produced no output files in {source_dir}")

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for fpath in files:
            zf.write(fpath, arcname=fpath.name)


@celery_app.task(bind=True, name="tasks.run_download", max_retries=0)
def run_download(self, job_id: str, url: str, fmt: str, bitrate: str) -> None:
    """
    Main download task using spotdl — supports all formats and bitrates.

    Flow:
        queued → processing → done   (happy path)
        queued → processing → error  (any failure)

    Redis job hash fields:
        status       — queued | processing | done | error | downloaded
        progress     — 0-100 integer string
        filename     — final filename (set on done)
        download_url — presigned R2 URL (set on done)
        object_key   — R2 object key (set on done)
        error        — error message (set on error)
        created_at   — ISO-8601 UTC timestamp
    """
    r = _redis()
    tmp_dir = f"/tmp/{job_id}"

    _set_job_fields(
        r, job_id,
        status="processing",
        progress=0,
        created_at=datetime.now(tz=timezone.utc).isoformat(),
    )

    try:
        Path(tmp_dir).mkdir(parents=True, exist_ok=True)

        _write_cookies_if_needed()
        _write_spotdl_config_if_needed()

        url_type = detect_url_type(url)
        cli_args = build_spotdl_args(url, fmt, bitrate, tmp_dir, cookies_path=_COOKIES_PATH)

        _set_job_fields(r, job_id, progress=10)

        # Inject proxy via subprocess environment
        subprocess_env = os.environ.copy()
        proxy_url = os.environ.get("PROXY_URL", "").strip()
        if proxy_url:
            subprocess_env["HTTP_PROXY"] = proxy_url
            subprocess_env["HTTPS_PROXY"] = proxy_url
            subprocess_env["http_proxy"] = proxy_url
            subprocess_env["https_proxy"] = proxy_url

        # Playlists/albums get more time — 4 threads × longer track lists
        job_timeout = 1800 if url_type in ("playlist", "album") else 600

        result = subprocess.run(
            cli_args,
            capture_output=True,
            text=True,
            timeout=job_timeout,
            env=subprocess_env,
        )

        if result.returncode != 0:
            stderr_snippet = (result.stderr or "").strip()[-1000:]
            stdout_snippet = (result.stdout or "").strip()[-500:]
            # Check if any audio files were actually downloaded despite errors
            audio_extensions = {".mp3", ".flac", ".m4a", ".opus", ".ogg"}
            downloaded_files = [
                p for p in Path(tmp_dir).iterdir()
                if p.is_file() and p.suffix.lower() in audio_extensions
            ]
            if not downloaded_files:
                raise RuntimeError(
                    f"spotdl exited with code {result.returncode}. "
                    f"stderr: {stderr_snippet} stdout: {stdout_snippet}"
                )
            # Partial success — some tracks failed but others downloaded fine
        _set_job_fields(r, job_id, progress=60)

        # Determine what file to upload
        if url_type in ("playlist", "album"):
            zip_path = f"/tmp/{job_id}.zip"
            _zip_directory(tmp_dir, zip_path)
            local_upload_path = zip_path
            filename = f"{job_id}.zip"
        else:
            # Single track — find the one audio file
            audio_extensions = {".mp3", ".flac", ".m4a", ".opus", ".ogg"}
            candidates = [
                p for p in Path(tmp_dir).iterdir()
                if p.is_file() and p.suffix.lower() in audio_extensions
            ]
            if not candidates:
                raise RuntimeError(
                    f"spotdl produced no audio file in {tmp_dir}. "
                    f"stdout: {result.stdout[-500:]}"
                )
            local_upload_path = str(candidates[0])
            filename = candidates[0].name

        _set_job_fields(r, job_id, progress=70)

        object_key = f"{job_id}/{filename}"
        upload_file(local_upload_path, object_key)

        _set_job_fields(r, job_id, progress=90)

        presigned_url = generate_presigned_url(object_key, expiry_seconds=7200)

        _set_job_fields(
            r, job_id,
            status="done",
            progress=100,
            filename=filename,
            download_url=presigned_url,
            object_key=object_key,
        )

    except subprocess.TimeoutExpired:
        _set_job_fields(
            r, job_id,
            status="error",
            error="Download timed out after 10 minutes.",
        )
    except Exception as exc:
        _set_job_fields(
            r, job_id,
            status="error",
            error=str(exc)[:500],
        )
    finally:
        # Always clean up temp dir
        if Path(tmp_dir).exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        zip_path_candidate = f"/tmp/{job_id}.zip"
        if Path(zip_path_candidate).exists():
            try:
                os.remove(zip_path_candidate)
            except OSError:
                pass
