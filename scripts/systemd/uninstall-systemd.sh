#!/bin/bash
# Sendspin systemd uninstaller
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
        echo "Running in non-interactive mode - proceeding with uninstall" >&2
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

# Check root
[[ $EUID -ne 0 ]] && { echo -e "${R}Error:${N} Please run with sudo"; exit 1; }
USER=${SUDO_USER:-$(whoami)}
[[ -z "$USER" || "$USER" == "root" ]] && { echo -e "${R}Error:${N} Cannot determine user"; exit 1; }

echo -e "\n${B}${C}Sendspin Systemd Uninstaller${N}\n"

# Confirm
if ! prompt_yn "This will remove sendspin service and configuration. Continue?" "no"; then
    echo "Cancelled"
    exit 0
fi

# Stop and disable service
if systemctl is-active --quiet sendspin.service 2>/dev/null; then
    echo -e "${D}Stopping service...${N}"
    systemctl stop sendspin.service
fi

if systemctl is-enabled --quiet sendspin.service 2>/dev/null; then
    echo -e "${D}Disabling service...${N}"
    systemctl disable sendspin.service &>/dev/null
fi

# Remove systemd unit
if [[ -f /etc/systemd/system/sendspin.service ]]; then
    echo -e "${D}Removing service file...${N}"
    rm /etc/systemd/system/sendspin.service
    systemctl daemon-reload
fi

# Remove old config
if [[ -f /etc/default/sendspin ]]; then
    echo -e "${D}Removing old configuration...${N}"
    rm /etc/default/sendspin
fi

# Remove new JSON config
CONFIG_FILE="/home/$USER/.config/sendspin/settings-daemon.json"
if [[ -f "$CONFIG_FILE" ]]; then
    echo -e "${D}Removing configuration...${N}"
    rm -f "$CONFIG_FILE"
    # Remove directory if empty
    rmdir --ignore-fail-on-non-empty "/home/$USER/.config/sendspin" 2>/dev/null || true
fi

# Uninstall sendspin
if sudo -u "$USER" bash -l -c "command -v uv" &>/dev/null; then
    echo -e "${D}Uninstalling sendspin...${N}"
    sudo -u "$USER" bash -l -c "uv tool uninstall sendspin" 2>/dev/null || true
fi

echo -e "\n${G}✓${N} Uninstallation complete"
