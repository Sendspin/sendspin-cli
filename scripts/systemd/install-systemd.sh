#!/bin/bash
# Sendspin systemd installation
set -e

# Colors
C='\033[0;36m'; G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; B='\033[1m'; D='\033[2m'; N='\033[0m'

# Check root
[[ $EUID -ne 0 ]] && { echo -e "${R}Error:${N} Must run with sudo"; exit 1; }
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

# Check dependencies
echo -e "${D}Checking dependencies...${N}"

# Check libportaudio2
if ! ldconfig -p 2>/dev/null | grep -q libportaudio.so; then
    echo -e "${Y}Missing:${N} libportaudio2"
    if [[ -n "$PKG_MGR" ]]; then
        if [[ "$PKG_MGR" == "pacman" ]]; then
            CMD="pacman -S portaudio"
        else
            CMD="$PKG_MGR install libportaudio2"
        fi
        read -p "Install now? (sudo $CMD) [Y/n] " -n1 -r; echo
        if [[ ! $REPLY =~ ^[Nn]$ ]]; then
            sudo $CMD || { echo -e "${R}Failed${N}"; exit 1; }
        else
            echo -e "${R}Error:${N} libportaudio2 required. Install with: ${B}sudo $CMD${N}"; exit 1
        fi
    else
        echo -e "${R}Error:${N} libportaudio2 required. Install via your package manager."; exit 1
    fi
fi

# Check Python version
if command -v python3 &>/dev/null; then
    PY_VER=$(python3 --version 2>&1 | awk '{print $2}')
    PY_MAJ=$(echo "$PY_VER" | cut -d. -f1)
    PY_MIN=$(echo "$PY_VER" | cut -d. -f2)
    if [[ $PY_MAJ -lt 3 || ($PY_MAJ -eq 3 && $PY_MIN -lt 12) ]]; then
        echo -e "${R}Error:${N} Python 3.12+ required (found $PY_VER)"
        echo -e "To resolve: install Python 3.12+ via your package manager or pyenv"
        exit 1
    fi
else
    echo -e "${R}Error:${N} python3 not found"
    [[ -n "$PKG_MGR" ]] && echo -e "To resolve: install python, for example: ${B}sudo $PKG_MGR install python3${N}"
    exit 1
fi

# Check uv
if ! sudo -u "$USER" bash -l -c "command -v uv" &>/dev/null && \
   ! sudo -u "$USER" test -f "/home/$USER/.cargo/bin/uv" && \
   ! sudo -u "$USER" test -f "/home/$USER/.local/bin/uv"; then
    echo -e "${Y}Missing:${N} uv"
    read -p "Install now? (curl -LsSf https://astral.sh/uv/install.sh | sh) [Y/n] " -n1 -r; echo
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

# Configure
echo ""
read -p "Client name [$(hostname)]: " NAME
NAME=${NAME:-$(hostname)}

echo -e "\n${D}Available audio devices:${N}"
sudo -u "$USER" bash -c "/home/$USER/.local/bin/sendspin --list-audio-devices" 2>&1 | head -n -2
read -p "Audio device [default]: " DEVICE

echo ""
read -p "Server URL [Leave blank to auto-discover (recommended)]: " URL

# Save config
cat > /etc/default/sendspin << EOF
# Friendly name displayed on the Sendspin server
SENDSPIN_CLIENT_NAME=$NAME

# Audio device index or name prefix (leave empty for default)
SENDSPIN_AUDIO_DEVICE=$DEVICE

# WebSocket server URL (leave empty for auto-discovery via mDNS)
SENDSPIN_SERVER_URL=$URL

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
ExecStart=/bin/bash -c 'exec \$HOME/.local/bin/sendspin --headless \
    \${SENDSPIN_CLIENT_NAME:+--name "\${SENDSPIN_CLIENT_NAME}"} \
    \${SENDSPIN_AUDIO_DEVICE:+--audio-device "\${SENDSPIN_AUDIO_DEVICE}"} \
    \${SENDSPIN_SERVER_URL:+--url "\${SENDSPIN_SERVER_URL}"} \
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
read -p "Enable on boot? [Y/n] " -n1 -r; echo
[[ ! $REPLY =~ ^[Nn]$ ]] && systemctl enable sendspin.service &>/dev/null

read -p "Start now? [Y/n] " -n1 -r; echo
if [[ ! $REPLY =~ ^[Nn]$ ]]; then
    systemctl start sendspin.service
    echo -e "\n${G}✓${N} Service started"
fi

# Summary
echo -e "\n${B}Installation Complete${N}"
echo -e "${D}Config:${N}  /etc/default/sendspin"
echo -e "${D}Service:${N} systemctl {start|stop|status} sendspin"
echo -e "${D}Logs:${N}    journalctl -u sendspin -f\n"
