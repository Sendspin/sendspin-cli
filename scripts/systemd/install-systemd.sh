#!/bin/bash
# Sendspin systemd service installation
set -e

# Ensure output is visible even when piped
exec 2>&1

# Colors
C='\033[0;36m'; G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; B='\033[1m'; D='\033[2m'; N='\033[0m'

# Detect if running interactively
INTERACTIVE=true
if [ ! -t 0 ]; then
    # stdin is not a terminal (piped)
    if [ ! -c /dev/tty ]; then
        # No TTY available - fully non-interactive
        INTERACTIVE=false
        echo "Running in non-interactive mode - using defaults" >&2
    fi
fi

# Prompt for yes/no with configurable default
# Usage: prompt_yn "question" [default]
# default can be "yes" (default) or "no"
prompt_yn() {
    local question="$1"
    local default="${2:-yes}"
    
    if [ "$INTERACTIVE" = true ]; then
        if [ "$default" = "no" ]; then
            read -p "$question [y/N] " -n1 -r REPLY </dev/tty; echo
            [[ $REPLY =~ ^[Yy]$ ]]
        else
            read -p "$question [Y/n] " -n1 -r REPLY </dev/tty; echo
            [[ ! $REPLY =~ ^[Nn]$ ]]
        fi
    else
        echo "$question [auto: $default]"
        [ "$default" = "yes" ]
    fi
}

# Prompt for input with default value
# Usage: VAR=$(prompt_input "prompt text" "default value")
prompt_input() {
    local prompt="$1"
    local default="$2"
    if [ "$INTERACTIVE" = true ]; then
        read -p "$prompt [$default]: " REPLY </dev/tty
        echo "${REPLY:-$default}"
    else
        echo "Using default for $prompt: $default"
        echo "$default"
    fi
}

# Install a package using the detected package manager
# Usage: install_package "canonical-package-name"
# Handles package name mapping for different distros
install_package() {
    local canonical_name="$1"
    local pkg_name="$canonical_name"  # default to canonical name
    
    # Map canonical package names to distro-specific names
    case "$PKG_MGR:$canonical_name" in
        pacman:libportaudio2) pkg_name="portaudio" ;;
        pacman:libopenblas0) pkg_name="openblas" ;;
        dnf:libopenblas0|yum:libopenblas0) pkg_name="openblas" ;;
        # Additional mappings can be added here as needed
    esac
    
    # Construct install command for the package manager
    local CMD=""
    case "$PKG_MGR" in
        pacman) CMD="pacman -S --noconfirm $pkg_name" ;;
        dnf|yum) CMD="$PKG_MGR install -y $pkg_name" ;;
        apt-get) CMD="$PKG_MGR install -y $pkg_name" ;;
        *) CMD="$PKG_MGR install -y $pkg_name" ;;
    esac
    
    if prompt_yn "Install now? ($CMD)"; then
        $CMD || { echo -e "${R}Failed${N}"; return 1; }
        return 0
    else
        echo -e "${R}Error:${N} Package required. Install with: ${B}$CMD${N}"
        return 1
    fi
}

# Check for root via sudo, detect original user to install properly.
[[ $EUID -ne 0 ]] && { echo -e "${R}Error:${N} Please run with sudo"; exit 1; }
USER=${SUDO_USER:-$(whoami)}
[[ -z "$USER" || "$USER" == "root" ]] && { echo -e "${R}Error:${N} Cannot determine user (installing as root is not recommended; log in as a user and run with sudo)"; exit 1; }

echo -e "\n${B}${C}Sendspin Service Installation${N}\n"

# Detect package manager
PKG_MGR=""
if command -v apt-get &>/dev/null; then PKG_MGR="apt-get"
elif command -v dnf &>/dev/null; then PKG_MGR="dnf"
elif command -v yum &>/dev/null; then PKG_MGR="yum"
elif command -v pacman &>/dev/null; then PKG_MGR="pacman"
fi

echo -e "${D}Checking dependencies...${N}"

# Check for and offer to install libportaudio2
if ! ldconfig -p 2>/dev/null | grep -q libportaudio.so; then
    echo -e "${Y}Missing:${N} libportaudio2"
    if [[ -n "$PKG_MGR" ]]; then
        install_package "libportaudio2" || exit 1
    else
        echo -e "${R}Error:${N} libportaudio2 required. Install via your package manager."
        exit 1
    fi
fi

# Check for and offer to install libopenblas0
if ! ldconfig -p 2>/dev/null | grep -q libopenblas.so; then
    echo -e "${Y}Missing:${N} libopenblas0"
    if [[ -n "$PKG_MGR" ]]; then
        install_package "libopenblas0" || exit 1
    else
        echo -e "${R}Error:${N} libopenblas0 required. Install via your package manager."
        exit 1
    fi
fi

# Check for and offer to install uv if needed
if ! sudo -u "$USER" bash -l -c "command -v uv" &>/dev/null && \
   ! sudo -u "$USER" test -f "/home/$USER/.cargo/bin/uv" && \
   ! sudo -u "$USER" test -f "/home/$USER/.local/bin/uv"; then
    echo -e "${Y}Missing:${N} uv"
    if prompt_yn "Install now? (curl -LsSf https://astral.sh/uv/install.sh | sh)"; then
        sudo -u "$USER" bash -c "curl -LsSf https://astral.sh/uv/install.sh | sh" || { echo -e "${R}Failed${N}"; exit 1; }
        echo -e "${G}✓${N} uv installed"
    else
        echo -e "${R}Error:${N} uv required. Install with: ${B}curl -LsSf https://astral.sh/uv/install.sh | sh${N}"; exit 1
    fi
fi

# Install or update sendspin
echo -e "\n${D}Installing sendspin...${N}"
if sudo -u "$USER" bash -l -c "uv tool list" 2>/dev/null | grep -q "^sendspin "; then
    echo -e "${D}Sendspin already installed, updating...${N}"
    sudo -u "$USER" bash -l -c "uv tool update sendspin" || { echo -e "${R}Failed${N}"; exit 1; }
else
    sudo -u "$USER" bash -l -c "uv tool install sendspin" || { echo -e "${R}Failed${N}"; exit 1; }
fi

# Grab the proper bin path from uv (in case it's non-standard)
SENDSPIN_BIN="$(sudo -u "$USER" bash -l -c "uv tool dir --bin")/sendspin"

# Function to generate client_id from name (convert to snake-case)
# e.g., "Kitchen Music Player" -> "kitchen-music-player"
generate_client_id() {
    local name="$1"
    # Convert to lowercase, replace spaces/special chars with hyphens, remove consecutive hyphens
    echo "$name" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]\+/-/g' | sed 's/^-\+\|-\+$//g'
}

# Load old config if it exists (for migration)
OLD_CONFIG="/etc/default/sendspin"
OLD_NAME=""
OLD_DEVICE=""
OLD_DELAY="0"

if [ -f "$OLD_CONFIG" ]; then
    echo -e "${D}Found existing config at $OLD_CONFIG, migrating...${N}"
    # Source the old config to get values
    source "$OLD_CONFIG"
    OLD_NAME="$SENDSPIN_CLIENT_NAME"
    OLD_DEVICE="$SENDSPIN_AUDIO_DEVICE"
    OLD_DELAY="${SENDSPIN_STATIC_DELAY_MS:-0}"
fi

# Configure
echo ""
NAME=$(prompt_input "Client name" "${OLD_NAME:-$(hostname)}")
CLIENT_ID=$(generate_client_id "$NAME")

echo -e "\n${D}Available audio devices:${N}"
# Detect user's session environment for accurate audio device listing
USER_UID=$(id -u "$USER")
USER_RUNTIME_DIR="/run/user/$USER_UID"
USER_DBUS=""

# Try to get DBUS address from user's session
if [ -d "$USER_RUNTIME_DIR" ]; then
    # Try to find dbus session from user's processes
    USER_DBUS=$(ps -u "$USER" e | grep -m1 'DBUS_SESSION_BUS_ADDRESS=' | sed 's/.*DBUS_SESSION_BUS_ADDRESS=\([^ ]*\).*/\1/' || true)
    [ -z "$USER_DBUS" ] && USER_DBUS="unix:path=$USER_RUNTIME_DIR/bus"
fi

# Run with user's environment
if [ -n "$USER_DBUS" ]; then
    sudo -u "$USER" env XDG_RUNTIME_DIR="$USER_RUNTIME_DIR" DBUS_SESSION_BUS_ADDRESS="$USER_DBUS" "$SENDSPIN_BIN" --list-audio-devices 2>&1 | head -n -2
else
    echo -e "${Y}Warning:${N} Cannot detect user session environment. Audio devices may not be accurate."
    sudo -u "$USER" "$SENDSPIN_BIN" --list-audio-devices 2>&1 | head -n -2
fi

DEVICE=$(prompt_input "Audio device" "${OLD_DEVICE:-default}")
[ "$DEVICE" = "default" ] && DEVICE=""

# Ask about MPRIS support
echo ""
echo -e "${D}MPRIS (Media Player Remote Interfacing Specification) enables playback control"
echo -e "(play/pause/next/prev) from input devices and system controllers.${N}"
echo -e "${D}Generally disabled for headless endpoints unless they have controls.${N}"
USE_MPRIS=false
prompt_yn "Enable MPRIS?" "no" && USE_MPRIS=true

# Create config directory
CONFIG_DIR="/home/$USER/.config/sendspin"
CONFIG_FILE="$CONFIG_DIR/settings-daemon.json"
sudo -u "$USER" mkdir -p "$CONFIG_DIR"

# Save config in new JSON format
# Create JSON with conditional fields (null if empty)
DEVICE_JSON="null"
[ -n "$DEVICE" ] && DEVICE_JSON="\"$DEVICE\""

# Use old delay value if it was set
DELAY_VALUE="${OLD_DELAY:-0.0}"

sudo -u "$USER" tee "$CONFIG_FILE" > /dev/null << EOF
{
  "name": "$NAME",
  "log_level": null,
  "listen_port": null,
  "player_volume": 25,
  "player_muted": false,
  "static_delay_ms": $DELAY_VALUE,
  "last_server_url": null,
  "client_id": "$CLIENT_ID",
  "audio_device": $DEVICE_JSON,
  "use_mpris": $USE_MPRIS
}
EOF

# Clean up old config file if it exists
if [ -f "$OLD_CONFIG" ]; then
    echo -e "${D}Removing old config file: $OLD_CONFIG${N}"
    rm -f "$OLD_CONFIG"
fi

# Install service
cat > /etc/systemd/system/sendspin.service << EOF
[Unit]
Description=Sendspin Multi-Room Audio Client
After=network-online.target sound.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
ExecStart=$SENDSPIN_BIN daemon
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only

[Install]
WantedBy=multi-user.target
EOF

chmod 644 /etc/systemd/system/sendspin.service
systemctl daemon-reload

# Enable and start
echo ""
prompt_yn "Enable on boot?" && systemctl enable sendspin.service &>/dev/null
prompt_yn "Start now?" && systemctl start sendspin.service && echo -e "\n${G}✓${N} Service started"

# Summary
echo -e "\n${B}Installation Complete${N}"
echo -e "${D}Config:${N}  $CONFIG_FILE"
echo -e "${D}Service:${N} systemctl {start|stop|status} sendspin"
echo -e "${D}Logs:${N}    journalctl -u sendspin -f\n"
