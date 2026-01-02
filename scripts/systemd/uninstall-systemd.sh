#!/bin/bash
# Sendspin systemd uninstaller
set -e

# Colors
C='\033[0;36m'; G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; B='\033[1m'; D='\033[2m'; N='\033[0m'

# Check root
[[ $EUID -ne 0 ]] && { echo -e "${R}Error:${N} Must run with sudo"; exit 1; }
USER=${SUDO_USER:-$(whoami)}
[[ -z "$USER" || "$USER" == "root" ]] && { echo -e "${R}Error:${N} Cannot determine user"; exit 1; }

echo -e "\n${B}${C}Sendspin Systemd Uninstaller${N}\n"

# Confirm
read -p "This will remove sendspin service and configuration. Continue? [y/N] " -n1 -r; echo
[[ ! $REPLY =~ ^[Yy]$ ]] && { echo "Cancelled"; exit 0; }

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

# Remove config
if [[ -f /etc/default/sendspin ]]; then
    echo -e "${D}Removing configuration...${N}"
    rm /etc/default/sendspin
fi

# Uninstall sendspin
if sudo -u "$USER" bash -l -c "command -v uv" &>/dev/null; then
    echo -e "${D}Uninstalling sendspin...${N}"
    sudo -u "$USER" bash -l -c "uv tool uninstall sendspin" 2>/dev/null || true
fi

echo -e "\n${G}✓${N} Uninstallation complete"
