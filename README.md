# Spotify Downloader

Full-stack deployment guide. Follow in order.

---

## Services Required

| Service | Purpose |
|---|---|
| Upstash Redis | Celery broker + job status store |
| Cloudflare R2 | Temp file storage |
| Azure Container Apps | Flask API + Celery worker |
| Vercel | Static frontend |

---

## 1 — Upstash Redis

1. Create a free Redis database at https://upstash.com
2. Copy the **TLS connection URL** (`rediss://...`)
3. Save as `REDIS_URL` — used in every backend container

---

## 2 — Cloudflare R2

1. Create a bucket named `spotify-downloads` (or your preferred name)
2. Generate an **R2 API token** with Object Read & Write permissions
3. Note your **Account ID** from the Cloudflare dashboard URL
4. Your R2 endpoint: `https://<account_id>.r2.cloudflarestorage.com`

---

## 3 — Cloudflare Worker (cleanup)

```bash
cd cloudflare-worker
npm install -g wrangler
wrangler login
# Edit wrangler.toml: set bucket_name to your actual bucket name
wrangler deploy
```

The worker runs every 30 minutes and deletes objects older than 30 minutes.

---

## 4 — Backend (Azure Container Apps)

### Build and push image

```bash
cd backend
az acr build \
  --registry <your-acr-name> \
  --image spotify-downloader:latest \
  .
```

### Deploy web container (Flask API)

```bash
az containerapp create \
  --name spotify-downloader-web \
  --resource-group <rg> \
  --environment <env-name> \
  --image <acr>.azurecr.io/spotify-downloader:latest \
  --target-port 8000 \
  --ingress external \
  --min-replicas 1 \
  --max-replicas 3 \
  --env-vars \
    COMMAND=web \
    REDIS_URL=<upstash-redis-url> \
    R2_ENDPOINT_URL=<r2-endpoint> \
    R2_ACCESS_KEY_ID=<r2-key-id> \
    R2_SECRET_ACCESS_KEY=secretref:r2-secret \
    R2_BUCKET_NAME=spotify-downloads \
    FRONTEND_URL=https://<your-app>.vercel.app
```

### Deploy worker container (Celery)

```bash
az containerapp create \
  --name spotify-downloader-worker \
  --resource-group <rg> \
  --environment <env-name> \
  --image <acr>.azurecr.io/spotify-downloader:latest \
  --ingress disabled \
  --min-replicas 1 \
  --max-replicas 5 \
  --env-vars \
    COMMAND=worker \
    REDIS_URL=<upstash-redis-url> \
    R2_ENDPOINT_URL=<r2-endpoint> \
    R2_ACCESS_KEY_ID=<r2-key-id> \
    R2_SECRET_ACCESS_KEY=secretref:r2-secret \
    R2_BUCKET_NAME=spotify-downloads
```

Note the public URL of the web container — you need it for the frontend.

---

## 5 — Frontend (Vercel)

1. Open `frontend/script.js`
2. Set `BACKEND_URL` to your Azure web container URL:
   ```js
   const BACKEND_URL = "https://spotify-downloader-web.<hash>.azurecontainerapps.io";
   ```
3. Deploy:
   ```bash
   vercel --prod
   ```
4. Copy the Vercel deployment URL
5. Go back to your Azure web container and update `FRONTEND_URL` to the Vercel URL

---

## Environment Variables Reference

See `backend/.env.example` for full descriptions.

| Variable | Container | Description |
|---|---|---|
| `COMMAND` | web, worker | `web` or `worker` |
| `REDIS_URL` | web, worker | Upstash Redis TLS URL |
| `R2_ENDPOINT_URL` | web, worker | Cloudflare R2 S3 endpoint |
| `R2_ACCESS_KEY_ID` | web, worker | R2 API token access key |
| `R2_SECRET_ACCESS_KEY` | web, worker | R2 API token secret |
| `R2_BUCKET_NAME` | web, worker | R2 bucket name |
| `FRONTEND_URL` | web only | Vercel deployment URL (CORS) |

---

## Architecture

```
User browser (Vercel)
    │
    │  POST /api/download
    ▼
Flask API (Azure Container Apps — web)
    │
    │  Celery task enqueue
    ▼
Upstash Redis (broker + job status)
    │
    │  Task consumed
    ▼
Celery Worker (Azure Container Apps — worker)
    │
    ├── runs spotdl via subprocess
    ├── uploads result to Cloudflare R2
    └── writes presigned URL to Redis job hash
    
User browser polls GET /api/status/{job_id} every 2s
    │
    │  status == "done"
    ▼
Frontend shows presigned download link
    │
    │  user clicks download
    ▼
Frontend calls POST /api/confirm-download/{job_id}
    │
    ▼
Flask API deletes R2 object immediately

Cloudflare Worker (cron every 30min)
    └── deletes any orphaned objects older than 30min
```
