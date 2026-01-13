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
        if [[ "$PKG_MGR" == "pacman" ]]; then
            CMD="pacman -S --noconfirm portaudio"
        else
            CMD="$PKG_MGR install -y libportaudio2"
        fi
        if [ "$INTERACTIVE" = true ]; then
            read -p "Install now? ($CMD) [Y/n] " -n1 -r </dev/tty; echo
        else
            REPLY="y"
            echo "Auto-installing libportaudio2"
        fi
        if [[ ! $REPLY =~ ^[Nn]$ ]]; then
            $CMD || { echo -e "${R}Failed${N}"; exit 1; }
        else
            echo -e "${R}Error:${N} libportaudio2 required. Install with: ${B}$CMD${N}"; exit 1
        fi
    else
        echo -e "${R}Error:${N} libportaudio2 required. Install via your package manager."; exit 1
    fi
fi

# Check for and offer to install uv if needed
if ! sudo -u "$USER" bash -l -c "command -v uv" &>/dev/null && \
   ! sudo -u "$USER" test -f "/home/$USER/.cargo/bin/uv" && \
   ! sudo -u "$USER" test -f "/home/$USER/.local/bin/uv"; then
    echo -e "${Y}Missing:${N} uv"
    if [ "$INTERACTIVE" = true ]; then
        read -p "Install now? (curl -LsSf https://astral.sh/uv/install.sh | sh) [Y/n] " -n1 -r </dev/tty; echo
    else
        REPLY="y"
        echo "Auto-installing uv"
    fi
    if [[ ! $REPLY =~ ^[Nn]$ ]]; then
        sudo -u "$USER" bash -c "curl -LsSf https://astral.sh/uv/install.sh | sh" || { echo -e "${R}Failed${N}"; exit 1; }
        echo -e "${G}✓${N} uv installed"
    else
        echo -e "${R}Error:${N} uv required. Install with: ${B}curl -LsSf https://astral.sh/uv/install.sh | sh${N}"; exit 1
    fi
fi

# Install sendspin
echo -e "\n${D}Installing sendspin...${N}"
sudo -u "$USER" bash -l -c "uv tool install sendspin" || { echo -e "${R}Failed${N}"; exit 1; }

# Grab the proper bin path from uv (in case it's non-standard)
SENDSPIN_BIN="$(sudo -u "$USER" bash -l -c "uv tool dir --bin")/sendspin"

# Configure
echo ""
if [ "$INTERACTIVE" = true ]; then
    read -p "Client name [$(hostname)]: " NAME </dev/tty
else
    NAME=$(hostname)
    echo "Using default client name: $NAME"
fi
NAME=${NAME:-$(hostname)}

echo -e "\n${D}Available audio devices:${N}"
sudo -u "$USER" bash -c "$SENDSPIN_BIN --list-audio-devices" 2>&1 | head -n -2
if [ "$INTERACTIVE" = true ]; then
    read -p "Audio device [default]: " DEVICE </dev/tty
else
    DEVICE=""
    echo "Using default audio device"
fi

# Save config
cat > /etc/default/sendspin << EOF
# Friendly name displayed on the Sendspin server
SENDSPIN_CLIENT_NAME=$NAME

# Audio device index or name prefix (leave empty for default)
SENDSPIN_AUDIO_DEVICE=$DEVICE

# Playback delay in milliseconds (typically negative, e.g., -100)
SENDSPIN_STATIC_DELAY_MS=0

# Additional command-line arguments (e.g., --log-level DEBUG)
SENDSPIN_ARGS=
EOF

# Install service
cat > /etc/systemd/system/sendspin.service << EOF
[Unit]
Description=Sendspin Multi-Room Audio Client
After=network-online.target sound.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/default/sendspin
User=$USER
ExecStart=/bin/bash -c 'exec $SENDSPIN_BIN daemon \
    \${SENDSPIN_CLIENT_NAME:+--name "\${SENDSPIN_CLIENT_NAME}"} \
    \${SENDSPIN_AUDIO_DEVICE:+--audio-device "\${SENDSPIN_AUDIO_DEVICE}"} \
    --static-delay-ms \${SENDSPIN_STATIC_DELAY_MS:-0} \
    \${SENDSPIN_ARGS}'
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

chmod 644 /etc/systemd/system/sendspin.service /etc/default/sendspin
systemctl daemon-reload

# Enable and start
echo ""
if [ "$INTERACTIVE" = true ]; then
    read -p "Enable on boot? [Y/n] " -n1 -r </dev/tty; echo
else
    REPLY="y"
    echo "Auto-enabling service on boot"
fi
[[ ! $REPLY =~ ^[Nn]$ ]] && systemctl enable sendspin.service &>/dev/null

if [ "$INTERACTIVE" = true ]; then
    read -p "Start now? [Y/n] " -n1 -r </dev/tty; echo
else
    REPLY="y"
    echo "Auto-starting service"
fi
if [[ ! $REPLY =~ ^[Nn]$ ]]; then
    systemctl start sendspin.service
    echo -e "\n${G}✓${N} Service started"
fi

# Summary
echo -e "\n${B}Installation Complete${N}"
echo -e "${D}Config:${N}  /etc/default/sendspin"
echo -e "${D}Service:${N} systemctl {start|stop|status} sendspin"
echo -e "${D}Logs:${N}    journalctl -u sendspin -f\n"
