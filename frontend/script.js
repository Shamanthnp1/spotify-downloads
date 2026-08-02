/**
 * Spotify Downloader — Frontend Controller
 *
 * BACKEND_URL: Set this to your Azure Container Apps web container URL.
 * Example: "https://spotify-downloader-web.azurecontainerapps.io"
 */
const BACKEND_URL = "https://spotify-downloader-web.bluestone-03fb1426.eastus.azurecontainerapps.io";

const POLL_INTERVAL_MS = 5000;
const POLL_TIMEOUT_MS = 1800000; // 30 minutes — enough for large playlists

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
let _bulkWarningMsg = null; // persists bulk warning across fake progress ticks
let pollStartTime = null;
let activeJobId = null;

// ── Bitrate lock when FLAC selected ────────────────────────────────────────
// ── Continuous crawling progress ─────────────────────────────────────────────
// Advances every 400ms using an easing curve — fast early, slows near 95%.
// Never reaches 100% on its own. Snaps to 100% only when job is done.
let _fakeProgressTimer = null;
let _fakeProgressVal = 0;

const PROGRESS_LABELS = [
  { at: 0,  label: "Queuing download…" },
  { at: 15, label: "Fetching track info…" },
  { at: 30, label: "Searching for audio…" },
  { at: 50, label: "Downloading audio…" },
  { at: 70, label: "Converting format…" },
  { at: 85, label: "Almost done…" },
  { at: 93, label: "Finishing up…" },
];

function _labelForProgress(pct) {
  let label = PROGRESS_LABELS[0].label;
  for (const entry of PROGRESS_LABELS) {
    if (pct >= entry.at) label = entry.label;
  }
  return label;
}

function startFakeProgress() {
  _fakeProgressVal = 0;
  progressBarFill.classList.remove("progress-bar-fill--indeterminate");
  clearInterval(_fakeProgressTimer);
  _setBar(0, null); // don't overwrite status text on start

  _fakeProgressTimer = setInterval(() => {
    if (_fakeProgressVal >= 95) return;
    const remaining = 95 - _fakeProgressVal;
    const increment = Math.max(0.15, remaining * 0.025);
    _fakeProgressVal = Math.min(95, _fakeProgressVal + increment);
    _setBar(_fakeProgressVal, _labelForProgress(_fakeProgressVal));
  }, 400);
}

function stopFakeProgress() {
  clearInterval(_fakeProgressTimer);
  _fakeProgressTimer = null;
}

function _setBar(pct, label) {
  const p = Math.max(0, Math.min(100, pct));
  progressBarFill.style.width = `${p}%`;
  progressBarTrack.setAttribute("aria-valuenow", Math.round(p));
  // Don't overwrite bulk warning until bar is past 15%
  if (label !== null && label !== undefined) {
    if (!_bulkWarningMsg || p >= 15) {
      statusText.textContent = label;
    }
  }
}
const FORMAT_HINTS = {
  mp3:  "Lossy — smaller file, universal compatibility",
  flac: "✦ Lossless — studio quality, larger file, bitrate N/A",
  m4a:  "Lossy — great quality on Apple devices",
  opus: "Lossy — best quality-to-size ratio",
};

const formatHint = document.getElementById("format-hint");

formatSelect.addEventListener("change", () => {
  const isFlac = formatSelect.value === "flac";
  bitrateSelect.disabled = isFlac;
  bitrateNote.textContent = isFlac ? "(lossless)" : "";
  formatHint.textContent = FORMAT_HINTS[formatSelect.value] || "";
  formatHint.className = "format-hint" + (isFlac ? " format-hint--lossless" : "");
});

// Set initial hint
formatHint.textContent = FORMAT_HINTS[formatSelect.value] || "";

// ── Form submission ─────────────────────────────────────────────────────────
form.addEventListener("submit", (evt) => {
  evt.preventDefault();
  submitDownload();
});

retryBtn.addEventListener("click", () => {
  resetUI();
});

// ── Result download click — allow re-download ──────────────────────────────

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

  let jobResponse;
  try {
    jobResponse = await enqueueJob(url, fmt, bitrate);
  } catch (err) {
    setFormLocked(false);
    hideProgress();
    showError(err.message || "Failed to reach the server. Please try again.");
    return;
  }

  const jobId = jobResponse.job_id;

  // Show bulk download warning as initial status label
  if (jobResponse.is_bulk) {
    const count = jobResponse.track_count;
    const msg = count
      ? `Playlist detected (${count} tracks) — may take 5–15 minutes`
      : "Playlist/album detected — may take 5–15 minutes";
    _bulkWarningMsg = msg;
    statusText.textContent = msg;
  } else {
    _bulkWarningMsg = null;
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

  return data;
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
      stopFakeProgress();
      _setBar(100, "Done!");
      setFormLocked(false);
      hideProgress();
      showResult(job.filename || "download", job.download_url);
      break;

    case "error":
      stopFakeProgress();
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
  progressBarFill.style.width = "0%";
  startFakeProgress();
}

function hideProgress() {
  progressSection.hidden = true;
  progressBarFill.classList.remove("progress-bar-fill--indeterminate");
  stopFakeProgress();
}

function setStatus(text, progress) {
  // Let the fake progress run — only update label from real status
  // Don't override bar width unless real progress is ahead of fake
  const currentWidth = parseFloat(progressBarFill.style.width) || 0;
  if (progress > currentWidth) {
    _setBar(progress, text);
  } else if (text) {
    statusText.textContent = text;
  }
}

function showResult(filename, downloadUrl) {
  // Auto-trigger the file download — no second click needed
  const a = document.createElement("a");
  a.href = downloadUrl;
  a.download = filename;
  a.style.display = "none";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);

  // Call confirm-download to mark job as done (best-effort, fire and forget)
  if (activeJobId) {
    confirmDownload(activeJobId).catch(() => {});
  }

  // Show a minimal success state — no Save File button needed
  resultFilename.textContent = `${filename} — download started`;
  resultDownloadLink.href = downloadUrl;
  resultDownloadLink.textContent = "Download again";
  resultSection.hidden = false;
}

function showError(message) {
  errorMessage.textContent = message;
  errorSection.hidden = false;
}

function resetUI() {
  clearTimeout(pollTimer);
  stopFakeProgress();
  _bulkWarningMsg = null;
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
