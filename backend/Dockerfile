FROM python:3.11-slim

# Prevent Python from writing .pyc files and buffering stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# ffmpeg required by spotdl for audio conversion
# Deno required by yt-dlp for certain YouTube video downloads
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        unzip \
        build-essential && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Install Deno system-wide
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# COMMAND env var controls which process this container runs:
#   web    → gunicorn WSGI server (Flask API)
#   worker → Celery worker
ENV COMMAND=web

CMD if [ "$COMMAND" = "worker" ]; then \
        celery -A tasks.celery_app worker \
            --loglevel=info \
            --concurrency=2 \
            --queues=celery \
            --without-heartbeat \
            --without-gossip \
            --without-mingle; \
    else \
        gunicorn app:app \
            --workers 4 \
            --bind 0.0.0.0:8000 \
            --timeout 120 \
            --access-logfile - \
            --error-logfile - \
            --log-level info; \
    fi
