import os
import re
import ssl
import uuid

import redis as redis_lib
from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from storage import delete_object

REDIS_URL = os.environ["REDIS_URL"]
FRONTEND_URL = os.environ.get("FRONTEND_URL", "*")

# Allow both bare domain and www variant
_allowed_origins = [FRONTEND_URL]
if FRONTEND_URL != "*":
    if FRONTEND_URL.startswith("https://www."):
        _allowed_origins.append(FRONTEND_URL.replace("https://www.", "https://"))
    elif not FRONTEND_URL.startswith("https://www."):
        _allowed_origins.append(FRONTEND_URL.replace("https://", "https://www."))
# Always allow the Vercel preview URL too
_allowed_origins.append("https://spotify-downloader-eta.vercel.app")

VALID_FORMATS = {"mp3", "flac", "m4a", "opus"}
VALID_BITRATES = {"128k", "192k", "256k", "320k"}
SPOTIFY_URL_RE = re.compile(
    r"https?://open\.spotify\.com/(track|playlist|album)/[A-Za-z0-9]+"
)

app = Flask(__name__)

CORS(app, origins=_allowed_origins)

limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    storage_uri=REDIS_URL,
    storage_options={"ssl_cert_reqs": ssl.CERT_NONE},
    default_limits=[],  # no global limit — applied per-route only
)


def _redis() -> redis_lib.Redis:
    return redis_lib.Redis.from_url(REDIS_URL, decode_responses=True, ssl_cert_reqs=ssl.CERT_NONE)


def _error(message: str, status: int):
    return jsonify({"error": message}), status


@app.route("/api/download", methods=["POST"])
@limiter.limit("20 per hour")
def start_download():
    body = request.get_json(silent=True)
    if not body:
        return _error("Request body must be JSON.", 400)

    url = body.get("url", "").strip()
    fmt = body.get("format", "").strip().lower()
    bitrate = body.get("bitrate", "").strip().lower()

    if not url:
        return _error("'url' is required.", 400)
    if not SPOTIFY_URL_RE.match(url):
        return _error(
            "Invalid URL. Only Spotify track URLs are accepted.", 400
        )

    # Only tracks supported — reject playlists and albums
    url_type_match = SPOTIFY_URL_RE.match(url)
    if url_type_match and url_type_match.group(1) in ("playlist", "album"):
        return _error(
            "Playlist and album downloads are temporarily unavailable. Please paste a single track URL.", 400
        )

    if fmt not in VALID_FORMATS:
        return _error(
            f"Invalid format '{fmt}'. Must be one of: {sorted(VALID_FORMATS)}.", 400
        )
    if fmt != "flac" and bitrate not in VALID_BITRATES:
        return _error(
            f"Invalid bitrate '{bitrate}'. Must be one of: {sorted(VALID_BITRATES)}.", 400
        )

    is_bulk = False
    track_count = None

    job_id = str(uuid.uuid4())

    r = _redis()
    r.hset(
        f"job:{job_id}",
        mapping={
            "status": "queued",
            "progress": "0",
        },
    )
    r.expire(f"job:{job_id}", 7200)

    # Import here to avoid circular at module load when running with gunicorn
    from tasks import run_download
    run_download.apply_async(args=[job_id, url, fmt, bitrate])

    return jsonify({"job_id": job_id, "is_bulk": bool(is_bulk), "track_count": track_count}), 202


@app.route("/api/status/<job_id>", methods=["GET"])
def get_status(job_id: str):
    if not _is_valid_uuid(job_id):
        return _error("Invalid job ID.", 400)

    r = _redis()
    job = r.hgetall(f"job:{job_id}")

    if not job:
        return _error("Job not found or expired.", 404)

    # Coerce progress to int for JSON consistency
    if "progress" in job:
        try:
            job["progress"] = int(job["progress"])
        except ValueError:
            job["progress"] = 0

    return jsonify(job), 200


@app.route("/api/confirm-download/<job_id>", methods=["POST"])
def confirm_download(job_id: str):
    if not _is_valid_uuid(job_id):
        return _error("Invalid job ID.", 400)

    r = _redis()
    job = r.hgetall(f"job:{job_id}")

    if not job:
        return _error("Job not found or expired.", 404)

    if job.get("status") not in ("done", "downloaded"):
        return _error(
            f"Job is not in a downloadable state (current status: {job.get('status')}).", 409
        )

    object_key = job.get("object_key", "")
    # Don't delete R2 object immediately — the presigned URL is still being
    # fetched by the browser. Mark as downloaded and let the cleanup Worker
    # handle deletion after the 2-hour TTL expires.
    r.hset(f"job:{job_id}", "status", "downloaded")
    r.expire(f"job:{job_id}", 7200)

    return jsonify({"message": "File deleted from storage. Thank you."}), 200


@app.route("/api/debug", methods=["GET"])
@limiter.exempt
def debug():
    """
    Diagnostic endpoint — tests SpotifyDown API connectivity.
    Remove before going to production.
    """
    import requests as req
    results = {}

    # Test SpotifyDown API
    try:
        resp = req.get(
            "https://api.spotifydown.com/download/11dFghVXANMlKmJXsNCbNl",
            headers={
                "Origin": "https://spotifydown.com",
                "Referer": "https://spotifydown.com/",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            },
            timeout=15,
        )
        data = resp.json()
        results["api_status"] = resp.status_code
        results["api_success"] = data.get("success", False)
        results["api_has_link"] = bool(data.get("link", ""))
        results["api_message"] = data.get("message", "")
    except Exception as exc:
        results["api_error"] = str(exc)

    return jsonify(results), 200



def rate_limit_exceeded(exc):
    return _error("Rate limit exceeded. Maximum 5 downloads per hour per IP.", 429)


@app.errorhandler(404)
def not_found(exc):
    return _error("Endpoint not found.", 404)


@app.errorhandler(405)
def method_not_allowed(exc):
    return _error("Method not allowed.", 405)


def _is_valid_uuid(value: str) -> bool:
    try:
        uuid.UUID(value, version=4)
        return True
    except ValueError:
        return False


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
