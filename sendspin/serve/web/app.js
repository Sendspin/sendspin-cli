/**
 * Sendspin Embedded Player
 * Auto-connects to the server that serves this page.
 */

// DOM elements
const elements = {
  startCard: document.getElementById("start-card"),
  startBtn: document.getElementById("start-btn"),
  playerCard: document.getElementById("player-card"),
  muteBtn: document.getElementById("mute-btn"),
  muteIcon: document.getElementById("mute-icon"),
  volumeSlider: document.getElementById("volume-slider"),
  volumeValue: document.getElementById("volume-value"),
  syncStatus: document.getElementById("sync-status"),
  disconnectBtn: document.getElementById("disconnect-btn"),
  shareCard: document.getElementById("share-card"),
  qrCode: document.getElementById("qr-code"),
  shareBtn: document.getElementById("share-btn"),
  shareServerUrl: document.getElementById("share-server-url"),
  castLink: document.getElementById("cast-link"),
};

// Player instance
let player = null;
let syncUpdateInterval = null;

// Auto-derive server URL from current page location
const serverUrl = `${location.protocol}//${location.host}`;
elements.shareServerUrl.textContent = serverUrl;
elements.shareServerUrl.href = serverUrl;

/**
 * Initialize the Sendspin player (called after user interaction)
 */
async function initPlayer() {
  // Import sendspin-js from unpkg CDN
  const { SendspinPlayer } = await sdkImport;

  // Remove player ID generation + useOutputLatencyCompensation: true
  // when merged https://github.com/Sendspin/sendspin-js/pull/29
  const playerId = `sendspin-web-${Math.random()
    .toString(36)
    .substring(2, 10)}`;

  player = new SendspinPlayer({
    playerId,
    baseUrl: serverUrl,
    onStateChange: handleStateChange,
    useOutputLatencyCompensation: true,
  });

  try {
    await player.connect();
    // Start polling for sync status updates
    syncUpdateInterval = setInterval(updateSyncStatus, 500);
  } catch (err) {
    console.error("Connection failed:", err);
    elements.syncStatus.textContent = "Connection failed";
  }
}

/**
 * Update sync status display
 */
function updateSyncStatus() {
  if (!player) return;

  // SDK has no way to control reconnect logic or inform us of disconnects yet
  // But since we check this ever 500ms, it's good enough for now
  if (!player.isConnected) {
    disconnect();
    return;
  }

  const syncInfo = player.syncInfo;
  if (syncInfo?.syncErrorMs !== undefined) {
    const syncMs = syncInfo.syncErrorMs;
    elements.syncStatus.textContent = `Sync: ${syncMs.toFixed(1)}ms`;

    // Add visual indicator for good sync
    if (Math.abs(syncMs) < 10) {
      elements.syncStatus.classList.add("synced");
    } else {
      elements.syncStatus.classList.remove("synced");
    }
  }
}

/**
 * Handle player state changes
 */
function handleStateChange(state) {
  // State changes are handled, sync is polled separately
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

  // Reset UI
  elements.playerCard.classList.add("hidden");
  elements.startCard.classList.remove("hidden");
  elements.syncStatus.textContent = "-";
  elements.syncStatus.classList.remove("synced");
}

// Set up Cast link with server URL
elements.castLink.href = `https://sendspin.github.io/cast/?host=${encodeURIComponent(
  location.hostname,
)}`;

if (["localhost", "127.0.0.1"].includes(location.hostname)) {
  elements.shareCard.textContent = "Sharing disabled when visiting localhost";
}

// Start button - required for AudioContext to work
elements.startBtn.addEventListener("click", async () => {
  elements.startCard.classList.add("hidden");
  elements.playerCard.classList.remove("hidden");
  await initPlayer();
});

// Disconnect button
elements.disconnectBtn.addEventListener("click", disconnect);

// Mute button
elements.muteBtn.addEventListener("click", () => {
  if (!player) return;
  const newMuted = !player.muted;
  player.setMuted(newMuted);
  elements.muteIcon.textContent = newMuted ? "\u{1F507}" : "\u{1F50A}";
});

// Volume slider
elements.volumeSlider.addEventListener("input", () => {
  if (!player) return;
  const volume = parseInt(elements.volumeSlider.value, 10);
  player.setVolume(volume);
  elements.volumeValue.textContent = `${volume}%`;
});

const sdkImport = import(
  "https://unpkg.com/@music-assistant/sendspin-js@latest/dist/index.js"
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
