/**
 * Spotify Downloader — Frontend Controller
 *
 * BACKEND_URL: Set this to your Azure Container Apps web container URL.
 * Example: "https://spotify-downloader-web.azurecontainerapps.io"
 */
const BACKEND_URL = "https://spotify-downloader-web.bluestone-03fb1426.eastus.azurecontainerapps.io";

const POLL_INTERVAL_MS = 5000;
const POLL_TIMEOUT_MS = 600000; // 10 minutes

// ── Element references ──────────────────────────────────────────────────────
const form = document.getElementById("download-form");
const urlInput = document.getElementById("url-input");
const formatSelect = document.getElementById("format-select");
const bitrateSelect = document.getElementById("bitrate-select");
const bitrateNote = document.getElementById("bitrate-note");
const downloadBtn = document.getElementById("download-btn");
const btnLabel = downloadBtn.querySelector(".btn-label");

const progressSection = document.getElementById("progress-section");
const statusText = document.getElementById("status-text");
const progressBarFill = document.getElementById("progress-bar-fill");
const progressBarTrack = document.getElementById("progress-bar-track");

const resultSection = document.getElementById("result-section");
const resultFilename = document.getElementById("result-filename");
const resultDownloadLink = document.getElementById("result-download-link");

const errorSection = document.getElementById("error-section");
const errorMessage = document.getElementById("error-message");
const retryBtn = document.getElementById("retry-btn");

// ── State ───────────────────────────────────────────────────────────────────
let pollTimer = null;
let pollStartTime = null;
let activeJobId = null;

// ── Bitrate lock when FLAC selected ────────────────────────────────────────
formatSelect.addEventListener("change", () => {
  const isFlac = formatSelect.value === "flac";
  bitrateSelect.disabled = isFlac;
  bitrateNote.textContent = isFlac ? "(lossless — bitrate N/A)" : "";
});

// ── Form submission ─────────────────────────────────────────────────────────
form.addEventListener("submit", (evt) => {
  evt.preventDefault();
  submitDownload();
});

retryBtn.addEventListener("click", () => {
  resetUI();
});

// ── Result download click — confirm deletion after user triggers download ──
resultDownloadLink.addEventListener("click", () => {
  if (!activeJobId) return;
  // Fire-and-forget — inform backend the user downloaded so R2 object is deleted
  confirmDownload(activeJobId).catch(() => {
    // Non-critical — cleanup worker handles orphaned objects every 30 min
  });
});

// ── Core functions ──────────────────────────────────────────────────────────

async function submitDownload() {
  const url = urlInput.value.trim();
  const fmt = formatSelect.value;
  const bitrate = bitrateSelect.value;

  if (!url) {
    showInputError("Please paste a Spotify URL.");
    return;
  }

  if (!isValidSpotifyUrl(url)) {
    showInputError("Only Spotify track, playlist, or album URLs are accepted.");
    return;
  }

  clearInputError();
  setFormLocked(true);
  showProgress();
  setStatus("Queuing download…", 0);

  let jobId;
  try {
    jobId = await enqueueJob(url, fmt, bitrate);
  } catch (err) {
    setFormLocked(false);
    hideProgress();
    showError(err.message || "Failed to reach the server. Please try again.");
    return;
  }

  activeJobId = jobId;
  pollStartTime = Date.now();
  pollStatus(jobId);
}

async function enqueueJob(url, fmt, bitrate) {
  const response = await fetchWithTimeout(
    `${BACKEND_URL}/api/download`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, format: fmt, bitrate }),
    },
    15000
  );

  const data = await response.json();

  if (!response.ok) {
    throw new Error(data.error || `Server error ${response.status}`);
  }

  if (!data.job_id) {
    throw new Error("Server returned an invalid response.");
  }

  return data.job_id;
}

function pollStatus(jobId) {
  clearTimeout(pollTimer);

  if (Date.now() - pollStartTime > POLL_TIMEOUT_MS) {
    setFormLocked(false);
    hideProgress();
    showError(
      "The download is taking too long (over 10 minutes). Please try again or try a shorter playlist."
    );
    return;
  }

  fetchWithTimeout(`${BACKEND_URL}/api/status/${jobId}`, {}, 10000)
    .then((res) => {
      if (!res.ok) {
        throw new Error(`Status check failed: HTTP ${res.status}`);
      }
      return res.json();
    })
    .then((job) => {
      handleJobUpdate(jobId, job);
    })
    .catch((err) => {
      // Network blip — keep polling, don't give up immediately
      console.warn("Poll error:", err.message);
      pollTimer = setTimeout(() => pollStatus(jobId), POLL_INTERVAL_MS * 2);
    });
}

function handleJobUpdate(jobId, job) {
  const status = job.status || "unknown";
  const progress = typeof job.progress === "number" ? job.progress : parseInt(job.progress, 10) || 0;

  switch (status) {
    case "queued":
      setStatus("Queued — waiting for worker…", progress);
      pollTimer = setTimeout(() => pollStatus(jobId), POLL_INTERVAL_MS);
      break;

    case "processing":
      setStatus(getProgressLabel(progress), progress);
      pollTimer = setTimeout(() => pollStatus(jobId), POLL_INTERVAL_MS);
      break;

    case "done":
      setFormLocked(false);
      hideProgress();
      showResult(job.filename || "download", job.download_url);
      break;

    case "error":
      setFormLocked(false);
      hideProgress();
      showError(job.error || "An unknown error occurred during download.");
      break;

    case "downloaded":
      // Already consumed — shouldn't happen in normal flow
      setFormLocked(false);
      hideProgress();
      showError("This job has already been downloaded and cleaned up.");
      break;

    default:
      setStatus(`Status: ${status}`, progress);
      pollTimer = setTimeout(() => pollStatus(jobId), POLL_INTERVAL_MS);
      break;
  }
}

async function confirmDownload(jobId) {
  const res = await fetchWithTimeout(
    `${BACKEND_URL}/api/confirm-download/${jobId}`,
    { method: "POST" },
    10000
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || `Confirm failed: HTTP ${res.status}`);
  }
  return res.json();
}

// ── UI helpers ──────────────────────────────────────────────────────────────

function showProgress() {
  progressSection.hidden = false;
  resultSection.hidden = true;
  errorSection.hidden = true;
  progressBarFill.classList.add("progress-bar-fill--indeterminate");
}

function hideProgress() {
  progressSection.hidden = true;
  progressBarFill.classList.remove("progress-bar-fill--indeterminate");
}

function setStatus(text, progress) {
  statusText.textContent = text;

  const pct = Math.max(0, Math.min(100, progress));
  progressBarFill.style.width = `${pct}%`;
  progressBarTrack.setAttribute("aria-valuenow", pct);

  if (pct > 0) {
    progressBarFill.classList.remove("progress-bar-fill--indeterminate");
  }
}

function showResult(filename, downloadUrl) {
  resultFilename.textContent = filename;
  resultDownloadLink.href = downloadUrl;
  resultSection.hidden = false;
}

function showError(message) {
  errorMessage.textContent = message;
  errorSection.hidden = false;
}

function resetUI() {
  clearTimeout(pollTimer);
  activeJobId = null;
  pollStartTime = null;

  resultSection.hidden = true;
  errorSection.hidden = true;
  progressSection.hidden = true;
  progressBarFill.style.width = "0%";
  progressBarFill.classList.remove("progress-bar-fill--indeterminate");
  clearInputError();
  setFormLocked(false);
}

function setFormLocked(locked) {
  downloadBtn.disabled = locked;
  urlInput.disabled = locked;
  formatSelect.disabled = locked;
  // Bitrate keeps its own disabled state based on format selection
  if (!locked) {
    bitrateSelect.disabled = formatSelect.value === "flac";
  } else {
    bitrateSelect.disabled = true;
  }
  btnLabel.textContent = locked ? "Downloading…" : "Download";
}

function showInputError(message) {
  urlInput.classList.add("input--error");
  urlInput.setAttribute("aria-invalid", "true");
  const hint = document.getElementById("url-hint");
  hint.textContent = message;
  hint.style.color = "var(--color-error)";
}

function clearInputError() {
  urlInput.classList.remove("input--error");
  urlInput.removeAttribute("aria-invalid");
  const hint = document.getElementById("url-hint");
  hint.textContent = "Track, playlist, or album links supported";
  hint.style.color = "";
}

function getProgressLabel(progress) {
  if (progress < 15)  return "Starting download…";
  if (progress < 40)  return "Fetching track info…";
  if (progress < 65)  return "Downloading audio…";
  if (progress < 75)  return "Uploading to storage…";
  if (progress < 95)  return "Generating download link…";
  return "Almost done…";
}

// ── Utilities ───────────────────────────────────────────────────────────────

function isValidSpotifyUrl(url) {
  return /^https?:\/\/open\.spotify\.com\/(track|playlist|album)\/[A-Za-z0-9]+/.test(url);
}

/**
 * fetch() with a hard timeout.
 * Throws an Error with a human-readable message on timeout or network failure.
 */
async function fetchWithTimeout(url, options = {}, timeoutMs = 15000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const response = await fetch(url, { ...options, signal: controller.signal });
    return response;
  } catch (err) {
    if (err.name === "AbortError") {
      throw new Error("Request timed out. Check your connection and try again.");
    }
    if (!navigator.onLine) {
      throw new Error("No internet connection detected.");
    }
    throw new Error("Network error. Please try again.");
  } finally {
    clearTimeout(timer);
  }
}
