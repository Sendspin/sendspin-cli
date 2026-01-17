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
[[ $EUID -ne 0 ]] && { echo -e "${R}Error:${N} Please run with sudo or as root"; exit 1; }

echo -e "\n${B}${C}Sendspin Systemd Uninstaller${N}\n"

# Detect which user was used (check if sendspin user exists and has sendspin installed)
DAEMON_USER=""
USE_DEDICATED_USER=false

if id -u sendspin &>/dev/null && sudo -u sendspin bash -l -c "command -v uv" &>/dev/null 2>&1; then
    # Check if sendspin user has sendspin installed
    if sudo -u sendspin bash -l -c "uv tool list" 2>/dev/null | grep -q "^sendspin "; then
        DAEMON_USER="sendspin"
        USE_DEDICATED_USER=true
        echo -e "${D}Detected installation with dedicated 'sendspin' user${N}"
    fi
fi

# If no dedicated user found, check current/sudo user
if [ -z "$DAEMON_USER" ]; then
    CURRENT_USER=${SUDO_USER:-$(whoami)}
    if [[ "$CURRENT_USER" != "root" ]]; then
        if sudo -u "$CURRENT_USER" bash -l -c "uv tool list" 2>/dev/null | grep -q "^sendspin "; then
            DAEMON_USER="$CURRENT_USER"
            echo -e "${D}Detected installation with user: $DAEMON_USER${N}"
        fi
    fi
fi

# If still not found, warn user
if [ -z "$DAEMON_USER" ]; then
    echo -e "${Y}Warning:${N} Could not detect sendspin installation user"
    echo -e "${D}Will clean up service and config files if present${N}"
fi

# Confirm
echo ""
if ! prompt_yn "This will remove sendspin service, configuration, and optionally the user. Continue?" "no"; then
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
    rm -f /etc/default/sendspin
fi

# Uninstall sendspin and remove config for detected user
if [ -n "$DAEMON_USER" ]; then
    # Remove new JSON config
    CONFIG_DIR="/home/$DAEMON_USER/.config/sendspin"
    if [[ -d "$CONFIG_DIR" ]]; then
        echo -e "${D}Removing configuration...${N}"
        rm -rf "$CONFIG_DIR"
    fi
    
    # Uninstall sendspin
    if sudo -u "$DAEMON_USER" bash -l -c "command -v uv" &>/dev/null; then
        echo -e "${D}Uninstalling sendspin from $DAEMON_USER...${N}"
        sudo -u "$DAEMON_USER" bash -l -c "uv tool uninstall sendspin" 2>/dev/null || true
    fi
fi

# Offer to remove dedicated sendspin user if it exists
if [ "$USE_DEDICATED_USER" = true ] && id -u sendspin &>/dev/null; then
    echo ""
    if prompt_yn "Remove 'sendspin' system user and home directory?" "no"; then
        echo -e "${D}Removing sendspin user and home directory...${N}"
        userdel -r sendspin 2>/dev/null || userdel sendspin 2>/dev/null || true
        echo -e "${G}✓${N} Removed sendspin user"
    else
        echo -e "${D}Keeping sendspin user${N}"
    fi
fi

echo -e "\n${B}${G}Uninstallation Complete!${N}"
