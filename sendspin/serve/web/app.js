/**
 * Sendspin Party - Windows 95 / Winamp Edition
 * Auto-connects to the server that serves this page.
 */

// DOM elements
const elements = {
  // Start dialog
  startDialog: document.getElementById("start-dialog"),
  startBtn: document.getElementById("start-btn"),
  startCancel: document.getElementById("start-cancel"),
  startDialogClose: document.getElementById("start-dialog-close"),

  // Winamp window
  winampWindow: document.getElementById("winamp-window"),
  winampClose: document.getElementById("winamp-close"),
  winampTitle: document.getElementById("winamp-title"),
  winampTime: document.getElementById("winamp-time"),
  winampSync: document.getElementById("winamp-sync"),
  winampSongTitle: document.getElementById("winamp-song-title"),
  visualizerCanvas: document.getElementById("visualizer-canvas"),
  seekProgress: document.getElementById("seek-progress"),
  volumeSlider: document.getElementById("volume-slider"),
  playBtn: document.getElementById("play-btn"),
  stopBtn: document.getElementById("stop-btn"),
  muteBtn: document.getElementById("mute-btn"),
  muteIcon: document.getElementById("mute-icon"),

  // Share window
  shareWindow: document.getElementById("share-window"),
  shareClose: document.getElementById("share-close"),
  shareServerUrl: document.getElementById("share-server-url"),
  shareBtn: document.getElementById("share-btn"),
  qrCode: document.getElementById("qr-code"),
  castLink: document.getElementById("cast-link"),

  // About window
  aboutWindow: document.getElementById("about-window"),
  aboutClose: document.getElementById("about-close"),

  // Desktop icons
  iconWinamp: document.getElementById("icon-winamp"),
  iconShare: document.getElementById("icon-share"),
  iconAbout: document.getElementById("icon-about"),

  // Taskbar
  taskbarStart: document.getElementById("taskbar-start"),
  taskbarWinamp: document.getElementById("taskbar-winamp"),
  taskbarSync: document.getElementById("taskbar-sync"),
  trayTime: document.getElementById("tray-time"),

  // Start menu
  startMenu: document.getElementById("start-menu"),
  menuPlayer: document.getElementById("menu-player"),
  menuShare: document.getElementById("menu-share"),
  menuAbout: document.getElementById("menu-about"),
  menuDisconnect: document.getElementById("menu-disconnect"),
};

// Player instance
let player = null;
let syncUpdateInterval = null;
let timeUpdateInterval = null;
let visualizerInterval = null;
let playStartTime = null;
let isPlaying = false;

// Auto-derive server URL from current page location
const serverUrl = `${location.protocol}//${location.host}`;
elements.shareServerUrl.value = serverUrl;

/**
 * Initialize the Sendspin player (called after user interaction)
 */
async function initPlayer() {
  const { SendspinPlayer } = await sdkImport;

  player = new SendspinPlayer({ baseUrl: serverUrl });

  try {
    await player.connect();
    isPlaying = true;
    playStartTime = Date.now();

    syncUpdateInterval = setInterval(updateSyncStatus, 500);
    timeUpdateInterval = setInterval(updatePlayTime, 1000);
    startVisualizer();

    // Update UI
    elements.playBtn.style.color = "#00ff00";
    elements.winampTitle.textContent = "SENDSPIN - PLAYING";
  } catch (err) {
    console.error("Connection failed:", err);
    elements.winampSync.textContent = "ERR";
    elements.winampSync.classList.add("error");
  }
}

/**
 * Update sync status display
 */
function updateSyncStatus() {
  if (!player) return;

  if (!player.isConnected) {
    disconnect();
    return;
  }

  const syncInfo = player.syncInfo;
  if (syncInfo?.syncErrorMs !== undefined) {
    const syncMs = syncInfo.syncErrorMs;
    const syncText = `${syncMs >= 0 ? "+" : ""}${syncMs.toFixed(0)}ms`;

    elements.winampSync.textContent = syncText;
    elements.taskbarSync.textContent = syncText;

    if (Math.abs(syncMs) < 10) {
      elements.winampSync.classList.add("synced");
      elements.winampSync.classList.remove("error");
      elements.taskbarSync.classList.add("synced");
      elements.taskbarSync.classList.remove("error");
    } else {
      elements.winampSync.classList.remove("synced");
      elements.taskbarSync.classList.remove("synced");
    }
  }
}

/**
 * Update play time display
 */
function updatePlayTime() {
  if (!isPlaying || !playStartTime) return;

  const elapsed = Math.floor((Date.now() - playStartTime) / 1000);
  const minutes = Math.floor(elapsed / 60);
  const seconds = elapsed % 60;
  elements.winampTime.textContent = `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;

  // Animate seek bar (loops every 60 seconds for effect)
  const progress = (elapsed % 60) / 60 * 100;
  elements.seekProgress.style.width = `${progress}%`;
}

/**
 * Start the visualizer animation
 */
function startVisualizer() {
  const canvas = elements.visualizerCanvas;
  const ctx = canvas.getContext("2d");
  const bars = 16;
  const barWidth = canvas.width / bars;

  function draw() {
    ctx.fillStyle = "#000000";
    ctx.fillRect(0, 0, canvas.width, canvas.height);

    for (let i = 0; i < bars; i++) {
      // Random bar heights for fake visualization
      const height = isPlaying ? Math.random() * canvas.height * 0.9 + canvas.height * 0.1 : 2;

      // Green gradient
      const gradient = ctx.createLinearGradient(0, canvas.height, 0, canvas.height - height);
      gradient.addColorStop(0, "#004400");
      gradient.addColorStop(0.5, "#00aa00");
      gradient.addColorStop(1, "#00ff00");

      ctx.fillStyle = gradient;
      ctx.fillRect(i * barWidth + 1, canvas.height - height, barWidth - 2, height);
    }
  }

  visualizerInterval = setInterval(draw, 100);
  draw();
}

/**
 * Stop the visualizer
 */
function stopVisualizer() {
  if (visualizerInterval) {
    clearInterval(visualizerInterval);
    visualizerInterval = null;
  }
}

/**
 * Disconnect from the server
 */
function disconnect() {
  if (syncUpdateInterval) {
    clearInterval(syncUpdateInterval);
    syncUpdateInterval = null;
  }

  if (timeUpdateInterval) {
    clearInterval(timeUpdateInterval);
    timeUpdateInterval = null;
  }

  stopVisualizer();

  if (player) {
    player.disconnect();
    player = null;
  }

  // Reset state
  isPlaying = false;
  playStartTime = null;

  // Reset UI
  elements.winampWindow.classList.add("hidden");
  elements.startDialog.classList.remove("hidden");
  elements.winampSync.textContent = "-";
  elements.winampSync.classList.remove("synced", "error");
  elements.taskbarSync.textContent = "-";
  elements.taskbarSync.classList.remove("synced", "error");
  elements.winampTime.textContent = "00:00";
  elements.seekProgress.style.width = "0%";
  elements.playBtn.style.color = "#00ff00";
  elements.winampTitle.textContent = "SENDSPIN";
}

/**
 * Update taskbar clock
 */
function updateClock() {
  const now = new Date();
  elements.trayTime.textContent = now.toLocaleTimeString("en-US", {
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
  });
}

/**
 * Window management
 */
function showWindow(windowEl) {
  windowEl.classList.remove("hidden");
  bringToFront(windowEl);
}

function hideWindow(windowEl) {
  windowEl.classList.add("hidden");
}

function toggleWindow(windowEl) {
  if (windowEl.classList.contains("hidden")) {
    showWindow(windowEl);
  } else {
    hideWindow(windowEl);
  }
}

let highestZIndex = 100;
function bringToFront(windowEl) {
  highestZIndex++;
  windowEl.style.zIndex = highestZIndex;
}

/**
 * Make windows draggable
 */
function makeDraggable(windowEl, handleEl) {
  let isDragging = false;
  let startX, startY, startLeft, startTop;

  handleEl.addEventListener("mousedown", (e) => {
    if (e.target.tagName === "BUTTON") return;

    isDragging = true;
    document.body.classList.add("dragging");
    bringToFront(windowEl);

    const rect = windowEl.getBoundingClientRect();
    startX = e.clientX;
    startY = e.clientY;
    startLeft = rect.left;
    startTop = rect.top;

    // Remove transform for proper positioning
    windowEl.style.transform = "none";
    windowEl.style.left = `${startLeft}px`;
    windowEl.style.top = `${startTop}px`;
  });

  document.addEventListener("mousemove", (e) => {
    if (!isDragging) return;

    const dx = e.clientX - startX;
    const dy = e.clientY - startY;

    windowEl.style.left = `${startLeft + dx}px`;
    windowEl.style.top = `${startTop + dy}px`;
  });

  document.addEventListener("mouseup", () => {
    isDragging = false;
    document.body.classList.remove("dragging");
  });
}

// Set up Cast link with server URL
elements.castLink.href = `https://sendspin.github.io/cast/?host=${encodeURIComponent(
  location.hostname
)}`;

// Handle localhost sharing
if (["localhost", "127.0.0.1"].includes(location.hostname)) {
  const urlDisplay = elements.shareServerUrl.parentElement;
  if (urlDisplay) {
    urlDisplay.innerHTML = '<span style="color: #808080;">Sharing disabled on localhost</span>';
  }
}

// ============================================
// EVENT LISTENERS
// ============================================

// Start dialog
elements.startBtn.addEventListener("click", async () => {
  elements.startDialog.classList.add("hidden");
  elements.winampWindow.classList.remove("hidden");
  await initPlayer();
});

elements.startCancel.addEventListener("click", () => {
  elements.startDialog.classList.add("hidden");
});

elements.startDialogClose.addEventListener("click", () => {
  elements.startDialog.classList.add("hidden");
});

// Winamp window controls
elements.winampClose.addEventListener("click", () => {
  disconnect();
});

elements.stopBtn.addEventListener("click", () => {
  disconnect();
});

elements.playBtn.addEventListener("click", async () => {
  if (!player) {
    await initPlayer();
  }
});

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
});

// Share window
elements.shareClose.addEventListener("click", () => {
  hideWindow(elements.shareWindow);
});

elements.shareBtn.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(serverUrl);
    elements.shareBtn.textContent = "Copied!";
    setTimeout(() => {
      elements.shareBtn.textContent = "Copy";
    }, 2000);
  } catch (err) {
    // Fallback
    elements.shareServerUrl.select();
    document.execCommand("copy");
  }
});

// About window
elements.aboutClose.addEventListener("click", () => {
  hideWindow(elements.aboutWindow);
});

// Desktop icons (double click)
elements.iconWinamp.addEventListener("dblclick", () => {
  if (player) {
    showWindow(elements.winampWindow);
  } else {
    showWindow(elements.startDialog);
  }
});

elements.iconShare.addEventListener("dblclick", () => {
  showWindow(elements.shareWindow);
});

elements.iconAbout.addEventListener("dblclick", () => {
  showWindow(elements.aboutWindow);
});

// Taskbar
elements.taskbarWinamp.addEventListener("click", () => {
  if (player) {
    toggleWindow(elements.winampWindow);
  } else {
    showWindow(elements.startDialog);
  }
});

elements.taskbarStart.addEventListener("click", (e) => {
  e.stopPropagation();
  toggleWindow(elements.startMenu);
});

// Start menu items
elements.menuPlayer.addEventListener("click", () => {
  hideWindow(elements.startMenu);
  if (player) {
    showWindow(elements.winampWindow);
  } else {
    showWindow(elements.startDialog);
  }
});

elements.menuShare.addEventListener("click", () => {
  hideWindow(elements.startMenu);
  showWindow(elements.shareWindow);
});

elements.menuAbout.addEventListener("click", () => {
  hideWindow(elements.startMenu);
  showWindow(elements.aboutWindow);
});

elements.menuDisconnect.addEventListener("click", () => {
  hideWindow(elements.startMenu);
  disconnect();
});

// Close start menu when clicking elsewhere
document.addEventListener("click", (e) => {
  if (!elements.startMenu.contains(e.target) && e.target !== elements.taskbarStart) {
    hideWindow(elements.startMenu);
  }
});

// Click on windows to bring to front
[elements.winampWindow, elements.shareWindow, elements.aboutWindow, elements.startDialog].forEach(
  (win) => {
    win.addEventListener("mousedown", () => bringToFront(win));
  }
);

// Make windows draggable
const winampTitlebar = elements.winampWindow.querySelector(".winamp-titlebar");
const shareTitlebar = elements.shareWindow.querySelector(".win95-titlebar");
const aboutTitlebar = elements.aboutWindow.querySelector(".win95-titlebar");
const dialogTitlebar = elements.startDialog.querySelector(".win95-titlebar");

makeDraggable(elements.winampWindow, winampTitlebar);
makeDraggable(elements.shareWindow, shareTitlebar);
makeDraggable(elements.aboutWindow, aboutTitlebar);
makeDraggable(elements.startDialog, dialogTitlebar);

// ============================================
// INITIALIZATION
// ============================================

// Import SDK
const sdkImport = import(
  "https://unpkg.com/@music-assistant/sendspin-js@1.0/dist/index.js"
);

// QR Code generation
if (typeof qrcode !== "undefined") {
  const qr = qrcode(0, "M");
  qr.addData(location.href);
  qr.make();
  elements.qrCode.innerHTML = qr.createSvgTag({ cellSize: 3, margin: 2 });
}

// Update clock every second
updateClock();
setInterval(updateClock, 1000);

// Focus on start dialog
bringToFront(elements.startDialog);
