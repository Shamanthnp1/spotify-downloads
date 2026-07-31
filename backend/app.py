import os
import re
import uuid

import redis as redis_lib
from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from storage import delete_object

REDIS_URL = os.environ["REDIS_URL"]
FRONTEND_URL = os.environ.get("FRONTEND_URL", "*")

VALID_FORMATS = {"mp3", "flac", "m4a", "opus"}
VALID_BITRATES = {"128k", "192k", "256k", "320k"}
SPOTIFY_URL_RE = re.compile(
    r"https?://open\.spotify\.com/(track|playlist|album)/[A-Za-z0-9]+"
)

app = Flask(__name__)

CORS(app, origins=[FRONTEND_URL])

limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    storage_uri=REDIS_URL,
    default_limits=["5 per hour"],
)


def _redis() -> redis_lib.Redis:
    return redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)


def _error(message: str, status: int):
    return jsonify({"error": message}), status


@app.route("/api/download", methods=["POST"])
@limiter.limit("5 per hour")
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
            "Invalid URL. Only Spotify track, playlist, or album URLs are accepted.", 400
        )
    if fmt not in VALID_FORMATS:
        return _error(
            f"Invalid format '{fmt}'. Must be one of: {sorted(VALID_FORMATS)}.", 400
        )
    if fmt != "flac" and bitrate not in VALID_BITRATES:
        return _error(
            f"Invalid bitrate '{bitrate}'. Must be one of: {sorted(VALID_BITRATES)}.", 400
        )
    if fmt == "flac" and bitrate and bitrate not in VALID_BITRATES:
        # bitrate is ignored for flac but don't fail on a supplied value
        pass

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

    return jsonify({"job_id": job_id}), 202


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
    if object_key:
        try:
            delete_object(object_key)
        except RuntimeError:
            # Best-effort delete — don't fail the response if R2 is flaky
            pass

    r.hset(f"job:{job_id}", "status", "downloaded")
    r.expire(f"job:{job_id}", 7200)

    return jsonify({"message": "File deleted from storage. Thank you."}), 200


@app.errorhandler(429)
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
