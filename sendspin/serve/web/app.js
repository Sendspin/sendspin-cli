/**
 * Sendspin Embedded Player
 * Auto-connects to the server that serves this page.
 */

const MAX_VOLUME = 100;
const SYNC_GRAPH_RANGE_MS = 50;
const SYNC_HISTORY_LENGTH = 180;
const GRAPH_SAMPLE_INTERVAL_MS = 45;
const UI_ACTIVATION_MS = 550;
const START_HAPTIC_PATTERN = [18, 28, 24];
const STOP_HAPTIC_PATTERN = [14];
const SYNC_CLASSES = ["sync-good", "sync-warn", "sync-bad", "sync-idle"];
const SYNC_PLACEHOLDER = "--.- ms";
const TONE_COLORS = {
  "sync-idle": [239, 225, 187],
  "sync-good": [245, 255, 246],
  "sync-warn": [255, 224, 130],
  "sync-bad": [255, 154, 146],
};

// DOM elements
const elements = {
  body: document.body,
  controlCard: document.getElementById("control-card"),
  listenToggleBtn: document.getElementById("listen-toggle-btn"),
  syncPanel: document.getElementById("sync-panel"),
  syncStatus: document.getElementById("sync-status"),
  syncGraphShell: document.getElementById("sync-graph-shell"),
  syncGraph: document.getElementById("sync-graph"),
  shareCard: document.getElementById("share-card"),
  qrCode: document.getElementById("qr-code"),
  shareBtn: document.getElementById("share-btn"),
  shareServerUrl: document.getElementById("share-server-url"),
  castLink: document.getElementById("cast-link"),
};

// Player instance and UI state
let player = null;
let syncUpdateInterval = null;
let syncGraphFrame = null;
let isListening = false;
let isStarting = false;
let showPostAnimationLabel = false;
let currentSyncMs = null;
let currentTone = "sync-idle";
let graphLastSampleAtMs = 0;
let graphHistory = [];

// Auto-derive server URL from current page location
const serverUrl = `${location.protocol}//${location.host}`;
elements.shareServerUrl.textContent = serverUrl;
elements.shareServerUrl.href = serverUrl;

function wait(ms) {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms);
  });
}

function triggerHaptic(pattern) {
  if (typeof navigator.vibrate !== "function") {
    return;
  }

  try {
    navigator.vibrate(pattern);
  } catch (err) {
    console.warn("Failed to trigger vibration:", err);
  }
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function rgba(color, alpha) {
  const [red, green, blue] = color;
  return `rgba(${red}, ${green}, ${blue}, ${alpha})`;
}

function formatSyncValue(syncMs) {
  const normalizedSyncMs = Math.abs(syncMs) < 0.05 ? 0 : syncMs;
  return `${normalizedSyncMs.toFixed(1)} ms`;
}

function getSyncTone(syncMs) {
  const absSyncMs = Math.abs(syncMs);
  if (absSyncMs < 10) {
    return "sync-good";
  }
  if (absSyncMs <= 25) {
    return "sync-warn";
  }
  return "sync-bad";
}

function clearGraphHistory() {
  graphHistory = [];
  graphLastSampleAtMs = 0;
}

function setSyncTone(tone) {
  currentTone = tone;
  elements.syncStatus.classList.remove(...SYNC_CLASSES);
  elements.syncGraphShell.classList.remove(...SYNC_CLASSES);
  elements.syncStatus.classList.add(tone);
  elements.syncGraphShell.classList.add(tone);
}

function setSyncDisplay({ label, tone = "sync-idle", syncMs = null }) {
  currentSyncMs = syncMs;
  elements.syncStatus.textContent = label;
  setSyncTone(tone);
}

function resetSyncDisplay() {
  setSyncDisplay({
    label: SYNC_PLACEHOLDER,
    tone: "sync-idle",
    syncMs: null,
  });
}

function updateUiState() {
  const pageIsActive = isListening || isStarting;

  elements.body.classList.toggle("is-listening", pageIsActive);
  elements.body.classList.toggle("is-starting", isStarting);
  elements.controlCard.classList.toggle("is-expanded", pageIsActive);
  elements.syncPanel.setAttribute("aria-hidden", String(!pageIsActive));
  elements.listenToggleBtn.setAttribute("aria-pressed", String(pageIsActive));

  if (isStarting && showPostAnimationLabel) {
    elements.listenToggleBtn.textContent = "Connecting...";
    return;
  }

  elements.listenToggleBtn.textContent = isListening
    ? "Stop Listening"
    : "Start Listening";
}

function handlePlayerStateChange() {
  if (!player) {
    return;
  }
  updateSyncStatus();
}

function getGraphContext() {
  const canvas = elements.syncGraph;
  const rect = canvas.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) {
    return null;
  }

  const dpr = window.devicePixelRatio || 1;
  const width = rect.width;
  const height = rect.height;
  const pixelWidth = Math.round(width * dpr);
  const pixelHeight = Math.round(height * dpr);

  if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
    canvas.width = pixelWidth;
    canvas.height = pixelHeight;
  }

  const ctx = canvas.getContext("2d");
  if (!ctx) {
    return null;
  }

  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, width, height };
}

function getGraphMetrics(width, height) {
  const left = 0;
  const right = 18;
  const top = 6;
  const bottom = 6;
  return {
    width,
    height,
    left,
    right,
    top,
    bottom,
    plotWidth: width - left - right,
    plotHeight: height - top - bottom,
  };
}

function getGraphX(index, historyLength, metrics) {
  const ageFromNewest = historyLength - 1 - index;
  const ratio = ageFromNewest / Math.max(SYNC_HISTORY_LENGTH - 1, 1);
  return metrics.width - metrics.right - ratio * metrics.plotWidth;
}

function getGraphY(syncMs, metrics) {
  const clamped = clamp(syncMs, -SYNC_GRAPH_RANGE_MS, SYNC_GRAPH_RANGE_MS);
  const ratio =
    (SYNC_GRAPH_RANGE_MS - clamped) / (SYNC_GRAPH_RANGE_MS * 2);
  return metrics.top + ratio * metrics.plotHeight;
}

function drawGraphGrid(ctx, metrics) {
  const lines = [
    { value: 50, label: "50", alpha: 0.16, dash: [] },
    { value: 25, label: null, alpha: 0.08, dash: [4, 6] },
    { value: 0, label: "0", alpha: 0.22, dash: [] },
    { value: -25, label: null, alpha: 0.08, dash: [4, 6] },
    { value: -50, label: "-50", alpha: 0.16, dash: [] },
  ];

  ctx.save();
  ctx.font = '11px "SF Mono", "Monaco", "Menlo", monospace';
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";

  for (const line of lines) {
    const y = getGraphY(line.value, metrics);
    ctx.beginPath();
    ctx.setLineDash(line.dash);
    ctx.moveTo(metrics.left, y);
    ctx.lineTo(metrics.width - metrics.right, y);
    ctx.strokeStyle = `rgba(255, 255, 255, ${line.alpha})`;
    ctx.lineWidth = line.value === 0 ? 1.2 : 1;
    ctx.stroke();

    if (line.label !== null) {
      const labelY = clamp(
        y + (line.value > 0 ? 12 : line.value < 0 ? -12 : -10),
        12,
        metrics.height - 12,
      );
      ctx.fillStyle = "rgba(255, 255, 255, 0.46)";
      ctx.fillText(line.label, metrics.width - 6, labelY);
    }
  }

  ctx.restore();
}

function traceSmoothLine(ctx, points) {
  if (points.length === 0) {
    return;
  }

  ctx.beginPath();
  ctx.moveTo(points[0].x, points[0].y);

  if (points.length === 1) {
    return;
  }

  for (let i = 1; i < points.length - 1; i += 1) {
    const midX = (points[i].x + points[i + 1].x) / 2;
    const midY = (points[i].y + points[i + 1].y) / 2;
    ctx.quadraticCurveTo(points[i].x, points[i].y, midX, midY);
  }

  const lastPoint = points[points.length - 1];
  ctx.quadraticCurveTo(lastPoint.x, lastPoint.y, lastPoint.x, lastPoint.y);
}

function drawSyncLine(ctx, metrics, history) {
  const toneColor = TONE_COLORS[currentTone] ?? TONE_COLORS["sync-idle"];
  const segments = [];
  let currentSegment = [];

  for (let i = 0; i < history.length; i += 1) {
    const sample = history[i];
    if (typeof sample.syncMs !== "number") {
      if (currentSegment.length > 0) {
        segments.push(currentSegment);
        currentSegment = [];
      }
      continue;
    }

    currentSegment.push({
      x: getGraphX(i, history.length, metrics),
      y: getGraphY(sample.syncMs, metrics),
    });
  }

  if (currentSegment.length > 0) {
    segments.push(currentSegment);
  }

  if (segments.length === 0) {
    return;
  }

  const strokeGradient = ctx.createLinearGradient(
    metrics.left,
    0,
    metrics.width - metrics.right,
    0,
  );
  strokeGradient.addColorStop(0, rgba(toneColor, 0.12));
  strokeGradient.addColorStop(0.7, rgba(toneColor, 0.58));
  strokeGradient.addColorStop(1, rgba(toneColor, 0.98));

  ctx.save();
  ctx.lineWidth = 2.5;
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  ctx.strokeStyle = strokeGradient;
  ctx.shadowBlur = 16;
  ctx.shadowColor = rgba(toneColor, 0.28);

  for (const segment of segments) {
    traceSmoothLine(ctx, segment);
    ctx.stroke();
  }

  ctx.restore();

  const lastSegment = segments[segments.length - 1];
  const lastPoint = lastSegment[lastSegment.length - 1];
  if (!lastPoint) {
    return;
  }

  ctx.save();
  ctx.beginPath();
  ctx.arc(lastPoint.x, lastPoint.y, 4.5, 0, Math.PI * 2);
  ctx.fillStyle = rgba(toneColor, 0.98);
  ctx.shadowBlur = 14;
  ctx.shadowColor = rgba(toneColor, 0.42);
  ctx.fill();
  ctx.restore();
}

function drawSyncGraph() {
  const graph = getGraphContext();
  if (!graph) {
    return;
  }

  const { ctx, width, height } = graph;
  const metrics = getGraphMetrics(width, height);

  ctx.clearRect(0, 0, width, height);
  drawGraphGrid(ctx, metrics);
  drawSyncLine(ctx, metrics, graphHistory);
}

function sampleGraphHistory() {
  graphHistory.push({
    syncMs: currentSyncMs,
  });

  if (graphHistory.length > SYNC_HISTORY_LENGTH) {
    graphHistory.shift();
  }
}

function syncGraphLoop(timestampMs) {
  if (graphLastSampleAtMs === 0) {
    graphLastSampleAtMs = timestampMs;
    sampleGraphHistory();
  }

  const elapsedMs = timestampMs - graphLastSampleAtMs;
  const resetThresholdMs = GRAPH_SAMPLE_INTERVAL_MS * SYNC_HISTORY_LENGTH;

  // After long tab suspension, drop stale history instead of replaying it.
  if (elapsedMs > resetThresholdMs) {
    clearGraphHistory();
    graphLastSampleAtMs = timestampMs;
    sampleGraphHistory();
  } else {
    while (timestampMs - graphLastSampleAtMs >= GRAPH_SAMPLE_INTERVAL_MS) {
      graphLastSampleAtMs += GRAPH_SAMPLE_INTERVAL_MS;
      sampleGraphHistory();
    }
  }

  drawSyncGraph();
  syncGraphFrame = window.requestAnimationFrame(syncGraphLoop);
}

function startSyncGraphLoop() {
  if (syncGraphFrame !== null) {
    return;
  }
  syncGraphFrame = window.requestAnimationFrame(syncGraphLoop);
}

/**
 * Initialize the Sendspin player (called after user interaction)
 */
async function initPlayer() {
  const { SendspinPlayer } = await sdkImport;

  player = new SendspinPlayer({
    baseUrl: serverUrl,
    onStateChange: handlePlayerStateChange,
  });

  try {
    await player.connect();
    if (syncUpdateInterval) {
      clearInterval(syncUpdateInterval);
    }
    syncUpdateInterval = setInterval(updateSyncStatus, 250);
  } catch (err) {
    if (player) {
      try {
        player.disconnect("user_request");
      } catch (disconnectErr) {
        console.warn("Failed to clean up after connection error:", disconnectErr);
      } finally {
        player = null;
      }
    }
    throw err;
  }
}

/**
 * Update sync status display
 */
function updateSyncStatus() {
  if (!player) {
    return;
  }

  if (!player.isConnected) {
    disconnect();
    return;
  }

  const syncInfo = player.syncInfo ?? {};
  const syncMs =
    typeof syncInfo.syncErrorMs === "number" &&
      Number.isFinite(syncInfo.syncErrorMs)
      ? syncInfo.syncErrorMs
      : null;

  if (!player.isPlaying || syncMs === null) {
    resetSyncDisplay();
    return;
  }

  setSyncDisplay({
    label: formatSyncValue(syncMs),
    tone: getSyncTone(syncMs),
    syncMs,
  });
}

async function startListening() {
  if (isListening || isStarting) {
    return;
  }

  isListening = true;
  isStarting = true;
  showPostAnimationLabel = false;
  elements.listenToggleBtn.disabled = true;
  clearGraphHistory();
  updateUiState();
  resetSyncDisplay();

  try {
    let connectPromise;

    if (player?.isConnected) {
      connectPromise = Promise.resolve();
    } else {
      if (player) {
        try {
          player.disconnect("user_request");
        } catch (disconnectErr) {
          console.warn("Failed to reset stale player before reconnect:", disconnectErr);
        } finally {
          player = null;
        }
      }
      connectPromise = initPlayer();
    }

    await wait(UI_ACTIVATION_MS);
    showPostAnimationLabel = true;
    updateUiState();

    await connectPromise;

    player.setVolume(MAX_VOLUME);
    player.setMuted(false);
    updateSyncStatus();
  } catch (err) {
    console.error("Connection failed:", err);
    disconnect();
  } finally {
    isStarting = false;
    showPostAnimationLabel = false;
    elements.listenToggleBtn.disabled = false;
    updateUiState();
  }
}

function stopListening() {
  isListening = false;
  isStarting = false;
  showPostAnimationLabel = false;

  if (player?.isConnected) {
    player.setMuted(true);
  }

  resetSyncDisplay();
  updateUiState();
}

/**
 * Disconnect from the server
 */
function disconnect() {
  if (syncUpdateInterval) {
    clearInterval(syncUpdateInterval);
    syncUpdateInterval = null;
  }

  if (player) {
    player.disconnect();
    player = null;
  }

  isListening = false;
  isStarting = false;
  showPostAnimationLabel = false;
  elements.listenToggleBtn.disabled = false;

  clearGraphHistory();
  resetSyncDisplay();
  updateUiState();
}

// Set up Cast link with server URL
elements.castLink.href = `https://sendspin.github.io/cast/?host=${encodeURIComponent(
  serverUrl,
)}`;

if (["localhost", "127.0.0.1"].includes(location.hostname)) {
  elements.shareCard.textContent = "Sharing disabled when visiting localhost";
}

elements.listenToggleBtn.addEventListener("click", async () => {
  if (isListening) {
    triggerHaptic(STOP_HAPTIC_PATTERN);
    stopListening();
    return;
  }

  triggerHaptic(START_HAPTIC_PATTERN);
  await startListening();
});

const sdkImport = import(
  "https://unpkg.com/@sendspin/sendspin-js@2.0.3/dist/index.js?module",
);

// QR Code generation (using qrcode-generator loaded via script tag)
if (typeof qrcode !== "undefined") {
  const qr = qrcode(0, "M");
  qr.addData(location.href);
  qr.make();
  elements.qrCode.innerHTML = qr.createSvgTag({ cellSize: 4, margin: 2 });
}

// Share button - copy URL to clipboard
elements.shareBtn.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(location.href);
  } catch (err) {
    // Fallback for browsers without clipboard API
    const textArea = document.createElement("textarea");
    textArea.value = location.href;
    document.body.appendChild(textArea);
    textArea.select();
    document.execCommand("copy");
    document.body.removeChild(textArea);
  }
  const origText = elements.shareBtn.textContent;
  elements.shareBtn.textContent = "Copied!";
  setTimeout(() => {
    elements.shareBtn.textContent = origText;
  }, 2000);
});

startSyncGraphLoop();
updateUiState();
resetSyncDisplay();
