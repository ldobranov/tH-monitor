#!/usr/bin/env python3
"""
WiFi configuration portal for Raspberry Pi Zero v1.1.

Design goals:
- Keep the config web page responsive while the device is in AP mode.
- Allow scanning for nearby infrastructure networks from the page.
- Save credentials without performing an immediate `nmcli dev wifi connect`
  test that tears down the current AP session on the single-radio wlan0.
- Perform the actual switch only when explicitly requested, with clear user
  messaging that the AP session will end on success.

This is intentionally conservative for Pi Zero hardware: one wireless radio
cannot reliably serve as AP and station at the same time in this workflow.
"""

from flask import Flask, render_template_string, request
import html
import logging
import os
import shlex
import subprocess
import threading
import time
from datetime import datetime

app = Flask(__name__)

LOG_FILE = '/home/raspberry/tH-monitor/wifi_safe_config.log'
PENDING_CONFIG_FILE = '/home/raspberry/tH-monitor/pending_wifi.env'
RUNTIME_SWITCH_SCRIPT = '/tmp/apply_saved_wifi.sh'
DEFAULT_FALLBACK_SSID = 'KavalaVIVA'

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s'
)
logger = logging.getLogger(__name__)

LAST_SCAN_RESULTS = []
STATE_LOCK = threading.Lock()
SWITCH_IN_PROGRESS = False

HTML_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <title>tH-Monitor WiFi Config</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {
            font-family: Arial, sans-serif;
            max-width: 640px;
            margin: 20px auto;
            padding: 16px;
            background-color: #f0f0f0;
        }
        .container {
            background: white;
            padding: 24px;
            border-radius: 10px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }
        h1 {
            color: #333;
            text-align: center;
            margin-top: 0;
        }
        .status {
            padding: 15px;
            margin: 18px 0;
            border-radius: 5px;
            text-align: center;
        }
        .status.success {
            background-color: #d4edda;
            color: #155724;
        }
        .status.error {
            background-color: #f8d7da;
            color: #721c24;
        }
        .status.warning {
            background-color: #fff3cd;
            color: #856404;
        }
        .status.info {
            background-color: #d1ecf1;
            color: #0c5460;
        }
        .panel {
            margin-bottom: 18px;
            padding: 14px;
            border-radius: 6px;
            background: #f8f9fa;
        }
        .panel h2 {
            margin: 0 0 10px 0;
            font-size: 18px;
        }
        .network-list {
            list-style: none;
            margin: 10px 0 0 0;
            padding: 0;
            max-height: 220px;
            overflow-y: auto;
            border: 1px solid #ddd;
            border-radius: 5px;
            background: #fff;
        }
        .network-list li {
            display: flex;
            justify-content: space-between;
            gap: 12px;
            padding: 10px 12px;
            border-bottom: 1px solid #eee;
            align-items: center;
        }
        .network-list li:last-child {
            border-bottom: none;
        }
        .network-meta {
            font-size: 12px;
            color: #666;
        }
        label {
            display: block;
            margin: 15px 0 5px;
            font-weight: bold;
        }
        input[type="text"], input[type="password"] {
            width: 100%;
            padding: 10px;
            border: 1px solid #ddd;
            border-radius: 5px;
            box-sizing: border-box;
        }
        button {
            width: 100%;
            padding: 14px;
            margin-top: 14px;
            color: white;
            border: none;
            border-radius: 5px;
            cursor: pointer;
            font-size: 16px;
        }
        button.primary {
            background-color: #4CAF50;
        }
        button.primary:hover {
            background-color: #45a049;
        }
        button.secondary {
            background-color: #1976d2;
        }
        button.secondary:hover {
            background-color: #1565c0;
        }
        button.warning {
            background-color: #ef6c00;
        }
        button.warning:hover {
            background-color: #e65100;
        }
        .small-button {
            width: auto;
            margin-top: 0;
            padding: 8px 12px;
            font-size: 13px;
        }
        .current-status {
            margin-bottom: 18px;
            padding: 12px;
            background: #e3f2fd;
            border-radius: 5px;
            line-height: 1.6;
        }
        .warning-box {
            background: #fff3cd;
            padding: 15px;
            border-radius: 5px;
            margin-bottom: 18px;
        }
        .hint {
            font-size: 13px;
            color: #555;
            margin-top: 8px;
        }
        .mono {
            font-family: Consolas, monospace;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>📶 tH-Monitor WiFi Config</h1>

        {% if status %}
        <div class="status {{ status_class }}">{{ status }}</div>
        {% endif %}

        <div class="current-status">
            <strong>Device Mode:</strong> {{ mode }}<br>
            <strong>Current Network:</strong> {{ current_ssid }}<br>
            <strong>IP Address:</strong> {{ ip_address }}<br>
            <strong>Pending Target:</strong> {{ pending_ssid }}
        </div>

        <div class="warning-box">
            <strong>⚠️ Raspberry Pi Zero v1.1 safe workflow</strong><br>
            While the Pi is serving this page from its own AP, it must not try to do a live test-connect on the same <span class="mono">wlan0</span> radio. Scan first, save the new WiFi credentials, then do one controlled switch.
        </div>

        <div class="panel">
            <h2>0. Enter AP / config mode</h2>
            <form method="POST" action="/start_ap">
                <button class="warning" type="submit">Start Config AP Now</button>
            </form>
            <div class="hint">Use this when the Pi is currently connected to a router and you want it to create its own setup hotspot.</div>
        </div>

        <div class="panel">
            <h2>1. Scan nearby WiFi networks</h2>
            <form method="POST" action="/scan">
                <button class="secondary" type="submit">Scan for Networks</button>
            </form>
            <div class="hint">This refreshes the visible SSID list without attempting to connect.</div>

            {% if networks %}
            <ul class="network-list">
                {% for network in networks %}
                <li>
                    <div>
                        <strong>{{ network.ssid }}</strong><br>
                        <span class="network-meta">Signal: {{ network.signal }} | Security: {{ network.security }}</span>
                    </div>
                    <form method="POST" action="/select">
                        <input type="hidden" name="ssid" value="{{ network.ssid }}">
                        <button class="secondary small-button" type="submit">Use</button>
                    </form>
                </li>
                {% endfor %}
            </ul>
            {% else %}
            <div class="hint">No scan results yet.</div>
            {% endif %}
        </div>

        <div class="panel">
            <h2>2. Save target WiFi credentials</h2>
            <form method="POST" action="/save">
                <label for="ssid">WiFi Network Name (SSID):</label>
                <input type="text" id="ssid" name="ssid" value="{{ ssid }}" required>

                <label for="password">WiFi Password:</label>
                <input type="password" id="password" name="password" value="{{ password }}" placeholder="Enter password">

                <button class="primary" type="submit">Save Credentials Only</button>
            </form>
            <div class="hint">This does not disconnect the current AP session.</div>
        </div>

        <div class="panel">
            <h2>3. Switch to the saved WiFi</h2>
            <form method="POST" action="/apply">
                <button class="warning" type="submit">Apply Saved WiFi and Leave AP Mode</button>
            </form>
            <div class="hint">After a successful switch, the current AP page will stop because the Pi will join the new router as a client.</div>
        </div>
    </div>
</body>
</html>
'''


def log_nmcli_state(stage, extra_details=None):
    """Capture WiFi state for field diagnostics."""
    try:
        logger.info("=== WIFI STATE SNAPSHOT: %s @ %s ===", stage, datetime.utcnow().isoformat())
        if extra_details:
            logger.info("EXTRA DETAILS: %s", extra_details)

        commands = {
            'device_status': ['nmcli', '-t', '-f', 'DEVICE,TYPE,STATE,CONNECTION', 'device', 'status'],
            'wifi_list': ['nmcli', '-t', '-f', 'IN-USE,SSID,SIGNAL,SECURITY', 'device', 'wifi', 'list', 'ifname', 'wlan0'],
            'active_connections': ['nmcli', '-t', '-f', 'NAME,UUID,TYPE,DEVICE', 'connection', 'show', '--active'],
            'all_connections': ['nmcli', '-t', '-f', 'NAME,UUID,TYPE,AUTOCONNECT', 'connection', 'show'],
            'ip_addr': ['hostname', '-I'],
        }

        for label, command in commands.items():
            result = subprocess.run(command, capture_output=True, text=True, timeout=12)
            logger.info('%s rc=%s stdout=%s stderr=%s', label, result.returncode, result.stdout.strip(), result.stderr.strip())
    except Exception as exc:
        logger.error("Failed to capture WiFi state snapshot '%s': %s", stage, str(exc))


def run_command(command, timeout=30):
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout)


def shell_quote(value):
    return shlex.quote(value)


def get_current_wifi_status():
    """Get current SSID/IP and infer whether device is in AP-like fallback state."""
    try:
        ip_result = run_command(['hostname', '-I'], timeout=5)
        ip_address = ip_result.stdout.strip() if ip_result.stdout.strip() else 'No IP'

        ssid_result = run_command(['nmcli', '-t', '-f', 'ACTIVE,SSID', 'dev', 'wifi'], timeout=5)
        current_ssid = 'Not connected'
        for line in ssid_result.stdout.splitlines():
            if line.startswith('yes:'):
                current_ssid = line.split(':', 1)[1] or 'Hidden SSID'
                break

        device_result = run_command(['nmcli', '-t', '-f', 'DEVICE,STATE,CONNECTION', 'device', 'status'], timeout=5)
        mode = 'Unknown'
        for line in device_result.stdout.splitlines():
            if line.startswith('wlan0:'):
                parts = line.split(':', 2)
                state = parts[1] if len(parts) > 1 else ''
                connection = parts[2] if len(parts) > 2 else ''
                if 'connected' in state and current_ssid != 'Not connected':
                    mode = 'Client mode'
                elif connection == '--' or 'disconnected' in state:
                    mode = 'AP / config mode'
                else:
                    mode = f'{state or "Unknown"}'
                break

        return current_ssid, ip_address, mode
    except Exception as exc:
        logger.error('Error getting WiFi status: %s', str(exc))
        return 'Not connected', 'No IP', 'Unknown'


def get_available_networks(force_rescan=False):
    """Return visible WiFi networks without changing the current connection state."""
    global LAST_SCAN_RESULTS

    try:
        if force_rescan:
            log_nmcli_state('pre-scan', {'force_rescan': True})
            run_command(['nmcli', 'dev', 'wifi', 'rescan', 'ifname', 'wlan0'], timeout=20)
            time.sleep(3)

        result = run_command(
            ['nmcli', '-t', '-f', 'SSID,SIGNAL,SECURITY', 'dev', 'wifi', 'list', 'ifname', 'wlan0'],
            timeout=20
        )

        networks = []
        seen = set()
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split(':')
            ssid = parts[0].strip() if len(parts) > 0 else ''
            signal = parts[1].strip() if len(parts) > 1 else '?'
            security = ':'.join(parts[2:]).strip() if len(parts) > 2 else 'UNKNOWN'
            if not ssid:
                continue
            key = (ssid, security)
            if key in seen:
                continue
            seen.add(key)
            networks.append({
                'ssid': ssid,
                'signal': signal or '?',
                'security': security or 'OPEN',
            })

        networks.sort(key=lambda item: (item['ssid'].lower(), -safe_int(item['signal'])))
        LAST_SCAN_RESULTS = networks
        logger.info('Scan result networks=%s', networks)
        return networks
    except Exception as exc:
        logger.error('Error scanning for networks: %s', str(exc))
        return LAST_SCAN_RESULTS


def safe_int(value):
    try:
        return int(value)
    except Exception:
        return -1


def read_pending_config():
    data = {'ssid': '', 'password': ''}
    if not os.path.exists(PENDING_CONFIG_FILE):
        return data

    try:
        with open(PENDING_CONFIG_FILE, 'r', encoding='utf-8') as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or '=' not in line:
                    continue
                key, value = line.split('=', 1)
                data[key.strip().lower()] = value.strip()
    except Exception as exc:
        logger.error('Failed to read pending config: %s', str(exc))
    return data


def write_pending_config(ssid, password):
    os.makedirs(os.path.dirname(PENDING_CONFIG_FILE), exist_ok=True)
    with open(PENDING_CONFIG_FILE, 'w', encoding='utf-8') as handle:
        handle.write(f'SSID={ssid}\n')
        handle.write(f'PASSWORD={password}\n')
    os.chmod(PENDING_CONFIG_FILE, 0o600)
    logger.info('Pending WiFi credentials updated for SSID=%s', ssid)


def remove_connection_if_exists(connection_name):
    existing = run_command(['nmcli', '-t', '-f', 'NAME', 'connection', 'show'], timeout=10)
    names = {line.strip() for line in existing.stdout.splitlines() if line.strip()}
    if connection_name in names:
        delete_result = run_command(['nmcli', 'connection', 'delete', connection_name], timeout=20)
        logger.info('Deleted existing connection %s rc=%s stderr=%s', connection_name, delete_result.returncode, delete_result.stderr.strip())


def build_connection_commands(ssid, password):
    connection_name = f'tH-monitor-{ssid}'
    quoted_ssid = shell_quote(ssid)
    quoted_password = shell_quote(password)
    quoted_connection = shell_quote(connection_name)

    commands = [
        f'nmcli connection delete {quoted_connection} >/dev/null 2>&1 || true',
        f'nmcli connection add type wifi con-name {quoted_connection} ifname wlan0 ssid {quoted_ssid}'
    ]

    if password:
        commands.append(
            f'nmcli connection modify {quoted_connection} wifi-sec.key-mgmt wpa-psk wifi-sec.psk {quoted_password}'
        )
    else:
        commands.append(
            f'nmcli connection modify {quoted_connection} 802-11-wireless-security.key-mgmt none >/dev/null 2>&1 || true'
        )

    commands.append(f'nmcli connection modify {quoted_connection} connection.autoconnect yes')
    commands.append(f'nmcli connection up {quoted_connection}')
    return connection_name, commands


def background_apply_saved_wifi(ssid, password):
    global SWITCH_IN_PROGRESS

    with STATE_LOCK:
        SWITCH_IN_PROGRESS = True

    try:
        log_nmcli_state('before-apply-saved-wifi', {'target_ssid': ssid})

        connection_name = f'tH-monitor-{ssid}'
        remove_connection_if_exists(connection_name)

        script_lines = [
            '#!/bin/bash',
            'set -e',
            'sleep 4',
            f'logger -t wifi_safe_config "Stopping AP and switching wlan0 to SSID {ssid}"',
            'pkill -f "hostapd /tmp/hostapd.conf" >/dev/null 2>&1 || true',
            'pkill -f "dnsmasq -C /tmp/dnsmasq.conf" >/dev/null 2>&1 || true',
            'rm -f /var/run/dnsmasq.pid >/dev/null 2>&1 || true',
            'systemctl restart NetworkManager >/dev/null 2>&1 || true',
            'sleep 3',
        ]
        _, connection_commands = build_connection_commands(ssid, password)
        script_lines.extend(connection_commands)
        script_lines.append(f'logger -t wifi_safe_config "WiFi switch command sequence completed for SSID {ssid}"')

        with open(RUNTIME_SWITCH_SCRIPT, 'w', encoding='utf-8') as handle:
            handle.write('\n'.join(script_lines) + '\n')
        os.chmod(RUNTIME_SWITCH_SCRIPT, 0o700)

        subprocess.Popen(['bash', RUNTIME_SWITCH_SCRIPT])
        logger.info('Launched background WiFi switch script for SSID=%s', ssid)
    except Exception as exc:
        logger.error('Failed to apply saved WiFi: %s', str(exc))
    finally:
        with STATE_LOCK:
            SWITCH_IN_PROGRESS = False


def render_page(status=None, status_class='info', ssid=None, password=''):
    pending = read_pending_config()
    current_ssid, ip_address, mode = get_current_wifi_status()
    display_ssid = pending.get('ssid', '') if ssid is None else ssid
    networks = LAST_SCAN_RESULTS
    return render_template_string(
        HTML_TEMPLATE,
        status=status,
        status_class=status_class,
        ssid=display_ssid,
        password=password,
        ip_address=ip_address,
        current_ssid=current_ssid,
        pending_ssid=pending.get('ssid', 'None saved') or 'None saved',
        mode=mode,
        networks=networks,
    )


def start_ap_mode():
    """Start AP mode from the web UI on Raspberry Pi OS Trixie systems."""
    try:
        log_nmcli_state('before-start-ap')
        script_lines = [
            '#!/bin/bash',
            'set -e',
            'sleep 2',
            'logger -t wifi_safe_config "Switching wlan0 into AP/config mode"',
            'nmcli radio wifi off >/dev/null 2>&1 || true',
            'pkill -f "hostapd /tmp/hostapd.conf" >/dev/null 2>&1 || true',
            'pkill -f "dnsmasq -C /tmp/dnsmasq.conf" >/dev/null 2>&1 || true',
            'rm -f /var/run/dnsmasq.pid >/dev/null 2>&1 || true',
            'systemctl stop wpa_supplicant >/dev/null 2>&1 || true',
            'systemctl stop NetworkManager >/dev/null 2>&1 || true',
            'ip link set wlan0 down >/dev/null 2>&1 || true',
            'ip addr flush dev wlan0 >/dev/null 2>&1 || true',
            'ip link set wlan0 up >/dev/null 2>&1 || true',
            'ip addr add 192.168.4.1/24 dev wlan0 >/dev/null 2>&1 || true',
            'hostapd /tmp/hostapd.conf -B >/dev/null 2>&1',
            'dnsmasq -C /tmp/dnsmasq.conf -x /var/run/dnsmasq.pid >/dev/null 2>&1',
            'logger -t wifi_safe_config "AP/config mode started on 192.168.4.1"',
        ]

        ap_bootstrap = [
            '#!/bin/bash',
            'cat > /tmp/hostapd.conf <<\'EOF\'',
            'interface=wlan0',
            'ssid=tH-Monitor-Config',
            'channel=1',
            'hw_mode=g',
            'ieee80211n=1',
            'wmm_enabled=1',
            'macaddr_acl=0',
            'ignore_broadcast_ssid=0',
            'EOF',
            'chmod 644 /tmp/hostapd.conf',
            'cat > /tmp/dnsmasq.conf <<\'EOF\'',
            'interface=wlan0',
            'bind-interfaces',
            'dhcp-range=192.168.4.2,192.168.4.20,255.255.255.0,24h',
            'domain=wlan',
            'address=/gw.wlan/192.168.4.1',
            'EOF',
            'chmod 644 /tmp/dnsmasq.conf',
        ]

        with open(RUNTIME_SWITCH_SCRIPT, 'w', encoding='utf-8') as handle:
            handle.write('\n'.join(ap_bootstrap + script_lines) + '\n')
        os.chmod(RUNTIME_SWITCH_SCRIPT, 0o700)
        subprocess.Popen(['bash', RUNTIME_SWITCH_SCRIPT])
        logger.info('Launched background AP mode switch script')
        return True, 'Started config AP. Reconnect your phone/laptop to SSID tH-Monitor-Config at 192.168.4.1.'
    except Exception as exc:
        logger.error('Failed to start AP mode: %s', str(exc))
        return False, f'Failed to start AP mode: {exc}'


@app.route('/')
def index():
    return render_page()


@app.route('/start_ap', methods=['POST'])
def start_ap():
    success, message = start_ap_mode()
    return render_page(status=message, status_class='warning' if success else 'error')


@app.route('/scan', methods=['POST'])
def scan_networks():
    networks = get_available_networks(force_rescan=True)
    if networks:
        return render_page(status=f'Found {len(networks)} network(s).', status_class='success')
    return render_page(status='No WiFi networks found during scan.', status_class='warning')


@app.route('/select', methods=['POST'])
def select_network():
    ssid = request.form.get('ssid', '').strip()
    if not ssid:
        return render_page(status='No SSID was selected.', status_class='error')
    pending = read_pending_config()
    return render_page(status=f'Selected network: {html.escape(ssid)}', status_class='info', ssid=ssid, password=pending.get('password', ''))


@app.route('/save', methods=['POST'])
def save_config():
    ssid = request.form.get('ssid', '').strip()
    password = request.form.get('password', '').strip()

    if not ssid:
        return render_page(status='Please enter a network name.', status_class='error', ssid='')

    write_pending_config(ssid, password)
    get_available_networks(force_rescan=False)
    return render_page(
        status=f'Saved WiFi credentials for {html.escape(ssid)}. AP is still active. Use the apply button when ready.',
        status_class='success',
        ssid=ssid,
        password=password,
    )


@app.route('/apply', methods=['POST'])
def apply_saved_wifi():
    pending = read_pending_config()
    ssid = pending.get('ssid', '').strip()
    password = pending.get('password', '')

    if not ssid:
        return render_page(status='No saved WiFi credentials found. Save a network first.', status_class='error')

    with STATE_LOCK:
        if SWITCH_IN_PROGRESS:
            return render_page(status='A WiFi switch is already in progress.', status_class='warning')
        thread = threading.Thread(target=background_apply_saved_wifi, args=(ssid, password), daemon=True)
        thread.start()

    return render_page(
        status=(
            f'Started switch to {html.escape(ssid)}. This page may disconnect in a few seconds. '
            'Reconnect the Pi from your normal router after the switch completes.'
        ),
        status_class='warning',
        ssid=ssid,
        password=password,
    )


if __name__ == '__main__':
    logger.info('Starting SAFE WiFi Configuration Server on port 8080...')
    logger.info('Access at: http://<raspberry-pi-ip>:8080')
    get_available_networks(force_rescan=False)
    app.run(host='0.0.0.0', port=8080, debug=False, threaded=True)
