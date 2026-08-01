import os
import ssl
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import redis as redis_lib
import requests
from celery import Celery

from downloader import (
    detect_url_type,
    extract_id,
    download_track,
    get_playlist_tracks,
    get_album_tracks,
    safe_filename,
)
from storage import upload_file, generate_presigned_url

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


def _redis() -> redis_lib.Redis:
    return redis_lib.Redis.from_url(REDIS_URL, decode_responses=True, ssl_cert_reqs=ssl.CERT_NONE)


def _set_job_fields(r: redis_lib.Redis, job_id: str, **fields) -> None:
    key = f"job:{job_id}"
    r.hset(key, mapping={k: str(v) for k, v in fields.items()})
    r.expire(key, JOB_TTL_SECONDS)


def _stream_to_file(url: str, dest_path: str) -> None:
    """
    Streams a remote URL to a local file.
    Raises RuntimeError on non-200 status or empty response.
    """
    resp = requests.get(url, stream=True, timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(f"Failed to download audio: HTTP {resp.status_code}")
    total = 0
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=65536):
            if chunk:
                f.write(chunk)
                total += len(chunk)
    if total == 0:
        raise RuntimeError("Downloaded file is empty")


@celery_app.task(bind=True, name="tasks.run_download", max_retries=0)
def run_download(self, job_id: str, url: str, fmt: str, bitrate: str) -> None:
    """
    Main download task using SpotifyDown API — no spotdl, no subprocess.

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
    tmp_dir = Path(f"/tmp/{job_id}")

    _set_job_fields(
        r, job_id,
        status="processing",
        progress=5,
        created_at=datetime.now(tz=timezone.utc).isoformat(),
    )

    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)

        url_type = detect_url_type(url)
        spotify_id = extract_id(url)

        _set_job_fields(r, job_id, progress=10)

        if url_type == "track":
            # Single track — one API call, stream to disk, upload to R2
            dl_link = download_track(spotify_id)
            _set_job_fields(r, job_id, progress=30)

            # Derive filename from URL path or use job_id as fallback
            url_filename = dl_link.split("/")[-1].split("?")[0]
            if not url_filename.endswith(".mp3"):
                url_filename = f"{job_id}.mp3"

            local_path = str(tmp_dir / url_filename)
            _stream_to_file(dl_link, local_path)
            _set_job_fields(r, job_id, progress=70)

            filename = url_filename
            upload_path = local_path

        else:
            # Playlist or album — fetch track list, download each, zip
            if url_type == "playlist":
                tracks = get_playlist_tracks(spotify_id)
            else:
                tracks = get_album_tracks(spotify_id)

            if not tracks:
                raise RuntimeError("No tracks found in playlist/album")

            total_tracks = len(tracks)
            _set_job_fields(r, job_id, progress=15)

            downloaded = 0
            for track in tracks:
                track_id = track.get("id", "")
                if not track_id:
                    continue
                try:
                    dl_link = download_track(track_id)
                    title = track.get("title", track_id)
                    artists = track.get("artists", "Unknown")
                    fname = safe_filename(title, artists)
                    local_path = str(tmp_dir / fname)
                    _stream_to_file(dl_link, local_path)
                    downloaded += 1
                    progress = 15 + int((downloaded / total_tracks) * 55)
                    _set_job_fields(r, job_id, progress=progress)
                except Exception:
                    # Skip failed tracks — don't abort the whole job
                    continue

            if downloaded == 0:
                raise RuntimeError("All tracks failed to download")

            # Zip everything
            zip_path = f"/tmp/{job_id}.zip"
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for p in tmp_dir.iterdir():
                    if p.is_file():
                        zf.write(str(p), arcname=p.name)

            _set_job_fields(r, job_id, progress=75)
            filename = f"{job_id}.zip"
            upload_path = zip_path

        # Upload to R2
        object_key = f"{job_id}/{filename}"
        upload_file(upload_path, object_key)
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

    except Exception as exc:
        _set_job_fields(
            r, job_id,
            status="error",
            error=str(exc)[:500],
        )
    finally:
        # Clean up temp files
        if tmp_dir.exists():
            import shutil
            shutil.rmtree(str(tmp_dir), ignore_errors=True)
        zip_candidate = Path(f"/tmp/{job_id}.zip")
        if zip_candidate.exists():
            zip_candidate.unlink(missing_ok=True)
