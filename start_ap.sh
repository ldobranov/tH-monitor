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

    cat > /tmp/hostapd.conf << EOF
    interface=wlan0
    driver=nl80211
    ssid=tH-Monitor-Config
    hw_mode=g
    channel=1
    ieee80211n=1
    wmm_enabled=1
    macaddr_acl=0
    ignore_broadcast_ssid=0
    EOF

    cat > /tmp/dnsmasq.conf << EOF
    interface=wlan0
    bind-interfaces
    dhcp-range=192.168.4.2,192.168.4.20,255.255.255.0,24h
    domain=wlan
    address=/gw.wlan/$AP_IP_PLAIN
    EOF

    chmod 644 /tmp/hostapd.conf /tmp/dnsmasq.conf
    log "Wrote AP config files"

    run_cmd rfkill unblock wifi || true

    # Do not call blocking systemctl stop/kill here. Just kill common processes.
    run_shell "pkill -9 -f NetworkManager >/dev/null 2>&1 || true"
    run_shell "pkill -9 -f wpa_supplicant >/dev/null 2>&1 || true"
    run_shell "pkill -9 -f dhcpcd >/dev/null 2>&1 || true"
    run_shell "pkill -9 -f hostapd >/dev/null 2>&1 || true"
    run_shell "pkill -9 -f dnsmasq >/dev/null 2>&1 || true"
    run_shell "rm -f /var/run/dnsmasq.pid || true"

    run_cmd ip link set wlan0 down || true
    run_cmd ip addr flush dev wlan0 || true
    run_cmd ip link set wlan0 up || true
    run_cmd ip addr add "$AP_IP" dev wlan0 || true

    run_cmd ip addr show wlan0 || true
    run_cmd iw dev wlan0 info || true

    run_cmd ls -l "$HOSTAPD_BIN" || true
    run_cmd ls -l "$DNSMASQ_BIN" || true
    run_cmd "$HOSTAPD_BIN" -B /tmp/hostapd.conf
    run_cmd "$DNSMASQ_BIN" --conf-file=/tmp/dnsmasq.conf --pid-file=/var/run/dnsmasq.pid

    sleep 2
    run_cmd pgrep -a hostapd || true
    run_cmd pgrep -a dnsmasq || true
    run_cmd ip addr show wlan0 || true
    run_cmd iw dev wlan0 info || true

    log "=== start_ap.sh end ==="
    echo "Access point 'tH-Monitor-Config' started on $AP_IP_PLAIN"
