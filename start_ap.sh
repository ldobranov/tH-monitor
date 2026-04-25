#!/bin/bash

# Minimal AP startup script for Raspberry Pi Zero / Raspberry Pi OS Trixie.
# Intention: avoid hanging service-manager calls that leave wlan0 dead.

set +e

LOG_FILE="/home/raspberry/tH-monitor/start_ap.log"
AP_IP="192.168.4.1/24"
AP_IP_PLAIN="192.168.4.1"
DNSMASQ_BIN="/usr/sbin/dnsmasq"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [start_ap] $1" >> "$LOG_FILE"
}

run_cmd() {
    log "RUN: $*"
    "$@" >> "$LOG_FILE" 2>&1
    local rc=$?
    log "RC=$rc CMD: $*"
    return $rc
}

run_shell() {
    log "RUN: $*"
    bash -lc "$*" >> "$LOG_FILE" 2>&1
    local rc=$?
    log "RC=$rc CMD: $*"
    return $rc
}

find_hostapd_bin() {
    local candidate
    for candidate in /usr/sbin/hostapd /usr/bin/hostapd /sbin/hostapd /bin/hostapd; do
        if [ -x "$candidate" ]; then
            echo "$candidate"
            return 0
        fi
    done

    candidate=$(command -v hostapd 2>/dev/null || true)
    if [ -n "$candidate" ]; then
        echo "$candidate"
        return 0
    fi

    return 1
}

log "=== start_ap.sh begin ==="

HOSTAPD_BIN="$(find_hostapd_bin || true)"
if [ -z "$HOSTAPD_BIN" ]; then
    log "ERROR: hostapd binary not found. Install hostapd package on the Pi."
    echo "ERROR: hostapd is not installed"
    exit 1
fi
log "Using hostapd binary: $HOSTAPD_BIN"

cat > /tmp/hostapd.conf << 'EOF'
interface=wlan0
driver=nl80211
ssid=tH-Monitor-Config
hw_mode=g
channel=6
ieee80211n=1
wmm_enabled=0
macaddr_acl=0
ignore_broadcast_ssid=0
EOF

cat > /tmp/dnsmasq.conf << EOF
# Don't use upstream DNS - we provide local DNS only
no-resolv

interface=wlan0
bind-interfaces
dhcp-range=192.168.4.2,192.168.4.20,255.255.255.0,24h
# DHCP options: router (option 3) and DNS (option 6)
dhcp-option=3,192.168.4.1
dhcp-option=6,192.168.4.1
# Set local domain suffix
local=/wlan/
domain=wlan
# Make dnsmasq authoritative for DHCP - responds faster to requests
dhcp-authoritative
# Log DHCP for debugging
log-dhcp

# Captive portal: resolve all known connectivity-check hosts to the AP gateway
# This causes phones to detect a captive portal and auto-open the config page
address=/#/$AP_IP_PLAIN
EOF

chmod 644 /tmp/hostapd.conf /tmp/dnsmasq.conf
log "Wrote AP config files"
log "hostapd.conf contents:"
cat /tmp/hostapd.conf >> "$LOG_FILE"
log "dnsmasq.conf contents:"
cat /tmp/dnsmasq.conf >> "$LOG_FILE"

# Determine if we need sudo (if not already root)
if [ "$(id -u)" -ne 0 ]; then
    SUDO="sudo"
    log "Not running as root, will use sudo for privileged commands"
else
    SUDO=""
    log "Running as root"
fi

$SUDO rfkill unblock wifi || true

# Mask NetworkManager so systemd won't restart it, then kill it
log "Masking and stopping NetworkManager..."
run_cmd $SUDO systemctl mask NetworkManager || true
run_cmd $SUDO systemctl mask wpa_supplicant || true
# Use kill directly instead of systemctl stop (avoids 30s timeout)
run_shell "$SUDO pkill -15 -f NetworkManager >/dev/null 2>&1 || true"
run_shell "$SUDO pkill -15 -f wpa_supplicant >/dev/null 2>&1 || true"
sleep 1
run_shell "$SUDO pkill -9 -f NetworkManager >/dev/null 2>&1 || true"
run_shell "$SUDO pkill -9 -f wpa_supplicant >/dev/null 2>&1 || true"

# Kill any remaining conflicting processes
run_shell "$SUDO pkill -9 -f NetworkManager >/dev/null 2>&1 || true"
run_shell "$SUDO pkill -9 -f wpa_supplicant >/dev/null 2>&1 || true"
run_shell "$SUDO pkill -9 -f dhcpcd >/dev/null 2>&1 || true"
run_shell "$SUDO pkill -9 -f hostapd >/dev/null 2>&1 || true"
run_shell "$SUDO pkill -9 -f dnsmasq >/dev/null 2>&1 || true"
run_shell "$SUDO rm -f /var/run/dnsmasq.pid || true"
sleep 2

run_cmd $SUDO ip link set wlan0 down || true
run_cmd $SUDO ip addr flush dev wlan0 || true
run_cmd $SUDO ip link set wlan0 up || true
run_cmd $SUDO ip addr add "$AP_IP" dev wlan0 || true

run_cmd ip addr show wlan0 || true
run_cmd iw dev wlan0 info || true

log "Starting hostapd..."
run_cmd $SUDO "$HOSTAPD_BIN" -B /tmp/hostapd.conf
HOSTAPD_RC=$?
log "hostapd exit code: $HOSTAPD_RC"

log "Starting dnsmasq..."
# Ensure lease file directory exists and is writable
$SUDO mkdir -p /var/lib/misc
$SUDO touch /var/lib/misc/dnsmasq.leases
$SUDO chmod 644 /var/lib/misc/dnsmasq.leases
run_cmd $SUDO "$DNSMASQ_BIN" --conf-file=/tmp/dnsmasq.conf --pid-file=/var/run/dnsmasq.pid
DNSMASQ_RC=$?
log "dnsmasq exit code: $DNSMASQ_RC"

# Captive portal: redirect HTTP (port 80) to our Flask config server (port 8080)
log "Setting up captive portal iptables redirect (80 -> 8080)..."
run_shell "$SUDO iptables -t nat -D PREROUTING -i wlan0 -p tcp --dport 80 -j REDIRECT --to-port 8080 >/dev/null 2>&1 || true"
run_shell "$SUDO iptables -t nat -A PREROUTING -i wlan0 -p tcp --dport 80 -j REDIRECT --to-port 8080"
log "Captive portal iptables rule added"

sleep 2
run_cmd pgrep -a hostapd || true
run_cmd pgrep -a dnsmasq || true
run_cmd ip addr show wlan0 || true
run_cmd iw dev wlan0 info || true

log "=== start_ap.sh end ==="
echo "Access point 'tH-Monitor-Config' started on $AP_IP_PLAIN"
