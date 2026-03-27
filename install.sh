#!/usr/bin/env bash
# pi-power-guard installer
# https://github.com/mahsumaktas/pi-power-guard
#
# Usage:
#   sudo bash install.sh
#   curl -fsSL https://raw.githubusercontent.com/mahsumaktas/pi-power-guard/main/install.sh | sudo bash

set -euo pipefail

VERSION="1.0.0"
INSTALL_DIR="/opt/pi-power-guard"
CONFIG_DIR="/etc/pi-power-guard"
SERVICE_NAME="pi-power-guard"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }
die()   { error "$@"; exit 1; }

echo ""
echo "========================================="
echo "  pi-power-guard v${VERSION} installer"
echo "========================================="
echo ""

# --- Pre-flight checks ---

[[ "$(id -u)" -eq 0 ]] || die "This installer must be run as root. Use: sudo bash install.sh"

# Check Pi 5
if [[ -f /proc/device-tree/model ]]; then
    MODEL=$(tr -d '\0' < /proc/device-tree/model)
    if [[ "$MODEL" != *"Raspberry Pi 5"* ]]; then
        die "This tool requires Raspberry Pi 5. Detected: ${MODEL}"
    fi
    info "Detected: ${MODEL}"
else
    die "Cannot detect Raspberry Pi model. Is this a Raspberry Pi?"
fi

# Check Python
if ! command -v python3 &>/dev/null; then
    die "Python 3 is required but not found"
fi

PYVER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PYMAJOR=$(echo "$PYVER" | cut -d. -f1)
PYMINOR=$(echo "$PYVER" | cut -d. -f2)
if [[ "$PYMAJOR" -lt 3 ]] || { [[ "$PYMAJOR" -eq 3 ]] && [[ "$PYMINOR" -lt 11 ]]; }; then
    die "Python 3.11+ required. Found: Python ${PYVER}"
fi
info "Python ${PYVER} found"

# Check vcgencmd
command -v vcgencmd &>/dev/null || die "vcgencmd not found. Is this Raspberry Pi OS?"

# Check pmic_read_adc (Pi 5 specific)
if ! vcgencmd pmic_read_adc &>/dev/null; then
    die "vcgencmd pmic_read_adc failed. This command requires Raspberry Pi 5."
fi
info "vcgencmd pmic_read_adc works"

# --- Determine source ---

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMP_DIR=""

if [[ -f "${SCRIPT_DIR}/pi_power_guard.py" ]]; then
    SOURCE_DIR="$SCRIPT_DIR"
    info "Installing from local directory: ${SOURCE_DIR}"
else
    TEMP_DIR=$(mktemp -d)
    info "Downloading from GitHub..."
    REPO_URL="https://raw.githubusercontent.com/mahsumaktas/pi-power-guard/main"
    for f in pi_power_guard.py config.ini pi-power-guard.service; do
        curl -fsSL "${REPO_URL}/${f}" -o "${TEMP_DIR}/${f}" || die "Failed to download ${f}"
    done
    SOURCE_DIR="$TEMP_DIR"
    info "Downloaded successfully"
fi

# --- Check for old pi-watchdog ---

if systemctl is-active --quiet pi-watchdog.service 2>/dev/null; then
    warn "Old pi-watchdog.service is running."
    read -rp "Stop and disable it? [Y/n] " ans
    ans=${ans:-Y}
    if [[ "${ans,,}" == "y" ]]; then
        systemctl stop pi-watchdog.service
        systemctl disable pi-watchdog.service
        rm -f /etc/systemd/system/pi-watchdog.service
        rm -f /usr/local/bin/pi-watchdog.py
        systemctl daemon-reload
        info "Old pi-watchdog removed"
    fi
fi

# --- Install files ---

info "Installing files..."

mkdir -p "$INSTALL_DIR"
cp "${SOURCE_DIR}/pi_power_guard.py" "${INSTALL_DIR}/pi_power_guard.py"
chmod 755 "${INSTALL_DIR}/pi_power_guard.py"

mkdir -p "$CONFIG_DIR"
if [[ -f "${CONFIG_DIR}/config.ini" ]]; then
    warn "Config already exists at ${CONFIG_DIR}/config.ini — keeping existing"
else
    cp "${SOURCE_DIR}/config.ini" "${CONFIG_DIR}/config.ini"
    info "Config installed to ${CONFIG_DIR}/config.ini"
fi

cp "${SOURCE_DIR}/pi-power-guard.service" /etc/systemd/system/
systemctl daemon-reload
info "systemd service installed"

# --- Optional: Hardware watchdog ---

CONFIG_TXT="/boot/firmware/config.txt"
if [[ -f "$CONFIG_TXT" ]]; then
    if ! grep -q "dtoverlay=watchdog" "$CONFIG_TXT" 2>/dev/null && \
       ! grep -q "dtoverlay=rp1-watchdog" "$CONFIG_TXT" 2>/dev/null; then
        echo ""
        read -rp "Enable hardware watchdog (dtoverlay=watchdog)? Recommended. [Y/n] " ans
        ans=${ans:-Y}
        if [[ "${ans,,}" == "y" ]]; then
            echo "" >> "$CONFIG_TXT"
            echo "# Hardware watchdog (added by pi-power-guard)" >> "$CONFIG_TXT"
            echo "dtoverlay=watchdog" >> "$CONFIG_TXT"
            info "Hardware watchdog enabled (reboot required to activate)"
        fi
    else
        info "Hardware watchdog already configured"
    fi
fi

# --- Optional: journald tuning ---

JOURNALD_CONF="/etc/systemd/journald.conf"
if [[ -f "$JOURNALD_CONF" ]]; then
    if ! grep -q "^SyncIntervalSec=" "$JOURNALD_CONF" 2>/dev/null; then
        echo ""
        read -rp "Set journald SyncIntervalSec=10s? Reduces log loss on crash. [Y/n] " ans
        ans=${ans:-Y}
        if [[ "${ans,,}" == "y" ]]; then
            # Add under [Journal] section
            sed -i '/^\[Journal\]/a SyncIntervalSec=10s' "$JOURNALD_CONF"
            # Ensure persistent storage
            if ! grep -q "^Storage=persistent" "$JOURNALD_CONF"; then
                sed -i '/^\[Journal\]/a Storage=persistent' "$JOURNALD_CONF"
            fi
            systemctl restart systemd-journald
            info "journald configured: SyncIntervalSec=10s, Storage=persistent"
        fi
    else
        info "journald SyncIntervalSec already configured"
    fi
fi

# --- Optional: systemd hardware watchdog ---

SYSCONF_DIR="/etc/systemd/system.conf.d"
if [[ ! -f "${SYSCONF_DIR}/watchdog.conf" ]]; then
    echo ""
    read -rp "Enable systemd RuntimeWatchdogSec=10? Reboots on kernel hang. [Y/n] " ans
    ans=${ans:-Y}
    if [[ "${ans,,}" == "y" ]]; then
        mkdir -p "$SYSCONF_DIR"
        cat > "${SYSCONF_DIR}/watchdog.conf" << 'WDEOF'
# Added by pi-power-guard
[Manager]
RuntimeWatchdogSec=10
ShutdownWatchdogSec=10min
WDEOF
        systemctl daemon-reexec
        info "systemd hardware watchdog enabled (RuntimeWatchdogSec=10)"
    fi
fi

# --- Enable and start ---

echo ""
info "Starting pi-power-guard..."
systemctl enable "$SERVICE_NAME"
systemctl start "$SERVICE_NAME"

echo ""
echo "========================================="
echo "  Installation complete!"
echo "========================================="
echo ""
systemctl status "$SERVICE_NAME" --no-pager -l 2>/dev/null || true
echo ""
info "Log file:    /var/log/pi-power-guard/current.log"
info "View logs:   tail -f /var/log/pi-power-guard/current.log"
info "Status:      systemctl status pi-power-guard"
info "Config:      ${CONFIG_DIR}/config.ini"
info "One-shot:    sudo python3 ${INSTALL_DIR}/pi_power_guard.py --check"
echo ""

if grep -q "dtoverlay=watchdog" "$CONFIG_TXT" 2>/dev/null; then
    warn "Reboot required to activate hardware watchdog"
fi

# Cleanup
[[ -n "$TEMP_DIR" ]] && rm -rf "$TEMP_DIR"
