#!/usr/bin/env bash
# pi-power-guard uninstaller
# https://github.com/mahsumaktas/pi-power-guard

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info() { echo -e "${GREEN}[INFO]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }

[[ "$(id -u)" -eq 0 ]] || { echo -e "${RED}[ERROR]${NC} Run as root: sudo bash uninstall.sh" >&2; exit 1; }

echo ""
echo "========================================="
echo "  pi-power-guard uninstaller"
echo "========================================="
echo ""

# Stop and disable service
if systemctl is-active --quiet pi-power-guard.service 2>/dev/null; then
    info "Stopping pi-power-guard..."
    systemctl stop pi-power-guard.service
fi

if systemctl is-enabled --quiet pi-power-guard.service 2>/dev/null; then
    systemctl disable pi-power-guard.service
fi

# Remove service file
rm -f /etc/systemd/system/pi-power-guard.service
systemctl daemon-reload
info "Service removed"

# Remove program files
rm -rf /opt/pi-power-guard
info "Program files removed"

# Ask about config
echo ""
read -rp "Remove configuration (/etc/pi-power-guard)? [y/N] " ans
if [[ "${ans,,}" == "y" ]]; then
    rm -rf /etc/pi-power-guard
    info "Configuration removed"
else
    info "Configuration kept at /etc/pi-power-guard/"
fi

# Ask about logs
read -rp "Remove log files (/var/log/pi-power-guard)? [y/N] " ans
if [[ "${ans,,}" == "y" ]]; then
    rm -rf /var/log/pi-power-guard
    info "Log files removed"
else
    info "Log files kept at /var/log/pi-power-guard/"
fi

# Clean state
rm -rf /var/lib/pi-power-guard
rm -rf /run/pi-power-guard

echo ""
info "pi-power-guard uninstalled successfully."
echo ""

# Note about optional configs
warn "Note: Hardware watchdog (config.txt) and journald settings were NOT reverted."
warn "Remove manually if desired:"
warn "  - /boot/firmware/config.txt: dtoverlay=watchdog"
warn "  - /etc/systemd/journald.conf: SyncIntervalSec"
warn "  - /etc/systemd/system.conf.d/watchdog.conf"
echo ""
