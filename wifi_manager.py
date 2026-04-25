#!/usr/bin/env python3
"""
Unified WiFi Manager Service for Raspberry Pi
Manages AP mode, client mode, LCD display, and web interface.
Runs as a systemd service for continuous operation.
"""

from flask import Flask, render_template, request, jsonify, redirect
import subprocess
import os
import time
import logging
import threading
import sys
import signal
import json
from pathlib import Path

# LCD access is delegated to lcd_service via the lcd_client library.
# This avoids I2C bus contention with monitor.py.
from lcd_client import LcdClient as _LcdClient
_lcd = _LcdClient('wifi_manager', default_priority=10)

app = Flask(__name__)

# Configuration
CONFIG_DIR = Path('/home/raspberry/tH-monitor')
LOG_FILE = CONFIG_DIR / 'wifi_manager.log'
PENDING_CONFIG_FILE = CONFIG_DIR / 'pending_wifi.env'
STATE_FILE = CONFIG_DIR / 'wifi_state.json'
AP_SSID = 'tH-Monitor-Config'
AP_IP = '192.168.4.1'
AP_PORT = 8080
MAX_AP_RETRIES = 3  # Maximum AP start attempts before giving up
AP_RETRY_FILE = CONFIG_DIR / 'ap_retry_count.txt'

# Setup logging
logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s'
)
logger = logging.getLogger(__name__)

# Global state
LAST_SCAN_RESULTS = []
AP_MODE_ACTIVE = False
CURRENT_MODE = 'unknown'  # 'ap', 'client', 'unknown'
STATE_LOCK = threading.Lock()

# Watchdog configuration
WATCHDOG_INTERVAL = 30       # seconds between checks
WATCHDOG_FAIL_THRESHOLD = 3  # consecutive failures before action
WATCHDOG_RECONNECT_TRIES = 2 # reconnect attempts before falling back to AP
WATCHDOG_ENABLED = True      # can be disabled via web UI

# Known Flask route paths – used by the captive portal before_request handler
KNOWN_ROUTES = frozenset([
    '/', '/scan', '/select', '/save', '/apply',
    '/reset_ap', '/watchdog_toggle', '/status',
    '/static/style.css',
])


def run_command(command, timeout=30):
    """Run a shell command and return result."""
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        logger.error('Command failed: %s - %s', command, str(exc))
        return None


def load_state():
    """Load state from file."""
    global CURRENT_MODE, AP_MODE_ACTIVE
    try:
        if STATE_FILE.exists():
            with open(STATE_FILE, 'r') as f:
                state = json.load(f)
                CURRENT_MODE = state.get('mode', 'unknown')
                AP_MODE_ACTIVE = state.get('ap_active', False)
    except Exception as e:
        logger.error('Failed to load state: %s', str(e))


def save_state():
    """Save state to file."""
    try:
        state = {
            'mode': CURRENT_MODE,
            'ap_active': AP_MODE_ACTIVE,
            'timestamp': time.time()
        }
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f)
    except Exception as e:
        logger.error('Failed to save state: %s', str(e))


def get_ap_retry_count():
    """Get current AP retry count."""
    try:
        if AP_RETRY_FILE.exists():
            with open(AP_RETRY_FILE, 'r') as f:
                return int(f.read().strip())
    except:
        pass
    return 0


def increment_ap_retry_count():
    """Increment AP retry count."""
    try:
        count = get_ap_retry_count() + 1
        with open(AP_RETRY_FILE, 'w') as f:
            f.write(str(count))
        return count
    except Exception as e:
        logger.error('Failed to increment AP retry count: %s', str(e))
        return 0


def reset_ap_retry_count():
    """Reset AP retry count to 0."""
    try:
        if AP_RETRY_FILE.exists():
            AP_RETRY_FILE.unlink()
    except Exception as e:
        logger.error('Failed to reset AP retry count: %s', str(e))


def get_current_wifi_status():
    """Get current SSID, IP address, and mode."""
    try:
        # Get IP address
        ip_result = run_command(['hostname', '-I'], timeout=5)
        ip_address = ip_result.stdout.strip() if ip_result and ip_result.stdout else 'No IP'

        # Get SSID
        ssid_result = run_command(['iwgetid', '-r'], timeout=5)
        current_ssid = ssid_result.stdout.strip() if ssid_result and ssid_result.stdout else 'Not connected'

        # Determine mode
        if AP_MODE_ACTIVE:
            mode = 'AP / config mode'
            mode_class = 'ap'
        elif current_ssid != 'Not connected':
            mode = 'Client mode'
            mode_class = 'client'
        else:
            mode = 'Unknown'
            mode_class = 'unknown'

        # Get service status
        service_result = run_command(['systemctl', 'is-active', 'wifi-manager.service'], timeout=5)
        service_status = service_result.stdout.strip() if service_result and service_result.stdout else 'unknown'

        return current_ssid, ip_address, mode, mode_class, service_status
    except Exception as exc:
        logger.error('Error getting WiFi status: %s', str(exc))
        return 'Not connected', 'No IP', 'Unknown', 'unknown', 'unknown'


def _parse_iw_scan(output):
    """Parse 'iw dev wlan0 scan dump' output into a list of network dicts."""
    networks = []
    seen = set()
    current_ssid = None
    current_signal = '?'
    current_security = 'OPEN'

    for line in output.splitlines():
        line = line.strip()
        if line.startswith('BSS '):
            if current_ssid:
                key = (current_ssid, current_security)
                if key not in seen:
                    seen.add(key)
                    networks.append({
                        'ssid': current_ssid,
                        'signal': current_signal,
                        'security': current_security,
                    })
            current_ssid = None
            current_signal = '?'
            current_security = 'OPEN'
        elif line.startswith('SSID:'):
            val = line[5:].strip()
            if val:
                current_ssid = val
        elif line.startswith('signal:'):
            try:
                current_signal = line.split(':')[1].strip().split(' ')[0]
            except Exception:
                current_signal = '?'
        elif 'WPA' in line or 'RSN' in line or 'WEP' in line:
            if 'WPA2' in line or 'RSN' in line:
                current_security = 'WPA2'
            elif 'WPA' in line:
                current_security = 'WPA'
            elif 'WEP' in line:
                current_security = 'WEP'

    # Save last entry
    if current_ssid:
        key = (current_ssid, current_security)
        if key not in seen:
            networks.append({
                'ssid': current_ssid,
                'signal': current_signal,
                'security': current_security,
            })

    return networks


def get_available_networks(force_rescan=False):
    """Scan for available WiFi networks.

    In AP mode NetworkManager is killed, so we use 'iw' directly.
    In client mode we prefer nmcli; if it returns nothing we fall back to iw.
    """
    global LAST_SCAN_RESULTS

    try:
        networks = []
        seen = set()

        if AP_MODE_ACTIVE:
            # ── AP mode: NetworkManager is not running, use iw ──────────────
            if force_rescan:
                logger.info('AP mode scan: using iw...')
                run_command(['iw', 'dev', 'wlan0', 'scan'], timeout=20)
                time.sleep(3)

            result = run_command(['iw', 'dev', 'wlan0', 'scan', 'dump'], timeout=20)
            if result and result.stdout:
                networks = _parse_iw_scan(result.stdout)
                logger.info('AP mode iw scan found %d networks', len(networks))

        else:
            # ── Client mode: try nmcli first ─────────────────────────────────
            if force_rescan:
                logger.info('Client mode scan: using nmcli...')
                run_command(['nmcli', 'dev', 'wifi', 'rescan', 'ifname', 'wlan0'], timeout=20)
                time.sleep(3)

            result = run_command(
                ['nmcli', '-t', '-f', 'SSID,SIGNAL,SECURITY', 'dev', 'wifi', 'list', 'ifname', 'wlan0'],
                timeout=20
            )
            if result and result.stdout:
                for line in result.stdout.splitlines():
                    if not line.strip():
                        continue
                    parts = line.split(':')
                    ssid = parts[0].strip() if parts else ''
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
                logger.info('nmcli scan found %d networks', len(networks))

            # Fallback to iw if nmcli returned nothing
            if not networks:
                logger.info('nmcli returned no results, falling back to iw...')
                if force_rescan:
                    run_command(['iw', 'dev', 'wlan0', 'scan'], timeout=20)
                    time.sleep(3)
                iw_result = run_command(['iw', 'dev', 'wlan0', 'scan', 'dump'], timeout=20)
                if iw_result and iw_result.stdout:
                    networks = _parse_iw_scan(iw_result.stdout)
                    logger.info('iw fallback scan found %d networks', len(networks))

        networks.sort(key=lambda item: (item['ssid'].lower(), -safe_float(item['signal'])))
        LAST_SCAN_RESULTS = networks
        logger.info('Scan complete, %d unique networks', len(networks))
        return networks
    except Exception as exc:
        logger.error('Error scanning for networks: %s', str(exc))
        return LAST_SCAN_RESULTS


def safe_int(value):
    """Safely convert to int."""
    try:
        return int(value)
    except Exception:
        return -1


def safe_float(value):
    """Safely convert to float (used for signal strength like -65.00 dBm)."""
    try:
        return float(value)
    except Exception:
        return -999.0


def read_pending_config():
    """Read pending WiFi configuration."""
    data = {'ssid': '', 'password': ''}
    if not PENDING_CONFIG_FILE.exists():
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
    """Write pending WiFi configuration."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(PENDING_CONFIG_FILE, 'w', encoding='utf-8') as handle:
        handle.write(f'SSID={ssid}\n')
        handle.write(f'PASSWORD={password}\n')
    os.chmod(str(PENDING_CONFIG_FILE), 0o600)
    logger.info('Saved WiFi credentials for SSID=%s', ssid)


def update_lcd_display(line1: str = '', line2: str = '', ttl: int = -1) -> None:
    """Send a display update to the LCD service.

    In AP mode uses priority 10 (overrides monitor.py indefinitely).
    In client mode uses priority 25 with TTL - monitor resumes after TTL seconds.
    Calling with empty strings clears this service's LCD slot.

    Args:
        line1: Top line text (max 16 chars)
        line2: Bottom line text (max 16 chars)
        ttl: Time-to-live in seconds. -1 means use default (5s for client mode, 0 for AP mode).
    """
    if not line1 and not line2:
        _lcd.clear()
        return

    # Determine TTL: -1 means use default
    if ttl == -1:
        ttl = 0 if AP_MODE_ACTIVE else 5

    if AP_MODE_ACTIVE:
        # AP mode - high priority, indefinite (no TTL)
        _lcd.write(line1, line2, priority=10, ttl=0)
    else:
        # Client mode - only show briefly when user is accessing wifi_manager
        # TTL means monitor will resume after that many seconds of no web activity
        _lcd.write(line1, line2, priority=25, ttl=ttl)


def start_ap_mode():
    """Start AP mode using the proven start_ap.sh script."""
    global AP_MODE_ACTIVE, CURRENT_MODE

    # Check retry count to prevent infinite restart loop
    retry_count = get_ap_retry_count()
    if retry_count >= MAX_AP_RETRIES:
        logger.warning('AP mode failed %d times, giving up to prevent lockout', retry_count)
        update_lcd_display('AP Failed', 'Too many retries', ttl=0)
        # Don't exit - stay in safe mode so user can recover via SSH
        return False, f'AP mode failed {retry_count} times. Service staying alive for recovery.'

    try:
        logger.info('Starting AP mode...')
        
        # First, disconnect from any existing WiFi connection
        logger.info('Disconnecting from any existing WiFi...')
        run_command(['nmcli', 'dev', 'disconnect', 'wlan0'], timeout=10)
        time.sleep(2)
        
        # Kill NetworkManager and wpa_supplicant processes (faster than systemctl in containers)
        logger.info('Stopping NetworkManager and wpa_supplicant...')
        run_command(['pkill', '-9', '-f', 'NetworkManager'], timeout=5)
        run_command(['pkill', '-9', '-f', 'wpa_supplicant'], timeout=5)
        run_command(['pkill', '-9', '-f', 'dhcpcd'], timeout=5)
        time.sleep(2)
        
        AP_MODE_ACTIVE = True
        CURRENT_MODE = 'ap'
        save_state()
        update_lcd_display('Starting AP...', 'Please wait', ttl=0)

        # Run the start_ap.sh script
        result = run_command(['bash', str(CONFIG_DIR / 'start_ap.sh')], timeout=60)

        if result and result.returncode == 0:
            logger.info('AP mode started successfully')
            reset_ap_retry_count()  # Reset retry count on success
            update_lcd_display('AP Mode Active', AP_IP, ttl=0)
            return True, f'AP mode started. Connect to SSID {AP_SSID} at {AP_IP}'
        else:
            error_msg = result.stderr.strip() if result and result.stderr else 'Unknown error'
            logger.error('Failed to start AP mode: %s', error_msg)
            AP_MODE_ACTIVE = False
            CURRENT_MODE = 'unknown'
            save_state()
            increment_ap_retry_count()  # Increment retry count
            update_lcd_display('AP Start Failed', 'Check logs', ttl=0)
            return False, f'Failed to start AP mode: {error_msg}'
    except Exception as exc:
        logger.error('Exception starting AP mode: %s', exc)
        AP_MODE_ACTIVE = False
        CURRENT_MODE = 'unknown'
        save_state()
        increment_ap_retry_count()  # Increment retry count
        update_lcd_display('AP Start Failed', 'Check logs', ttl=0)
        return False, f'Failed to start AP mode: {exc}'


def stop_ap_mode():
    """Stop AP mode and restore normal WiFi services."""
    global AP_MODE_ACTIVE, CURRENT_MODE

    try:
        logger.info('Stopping AP mode...')
        AP_MODE_ACTIVE = False
        CURRENT_MODE = 'client'
        save_state()
        update_lcd_display('Stopping AP...', '', ttl=3)

        # Kill AP services
        run_command(['pkill', '-9', '-f', 'hostapd'], timeout=10)
        run_command(['pkill', '-9', '-f', 'dnsmasq'], timeout=10)
        run_command(['rm', '-f', '/var/run/dnsmasq.pid'], timeout=5)

        # Remove captive portal iptables redirect rule
        run_command([
            'iptables', '-t', 'nat', '-D', 'PREROUTING',
            '-i', 'wlan0', '-p', 'tcp', '--dport', '80',
            '-j', 'REDIRECT', '--to-port', '8080'
        ], timeout=5)

        # Unmask and restart WiFi services
        logger.info('Unmasking and restarting WiFi services...')
        run_command(['systemctl', 'unmask', 'NetworkManager'], timeout=10)
        run_command(['systemctl', 'unmask', 'wpa_supplicant'], timeout=10)
        run_command(['pkill', '-9', '-f', 'NetworkManager'], timeout=5)
        run_command(['pkill', '-9', '-f', 'wpa_supplicant'], timeout=5)
        time.sleep(2)
        
        # Start services
        run_command(['systemctl', 'start', 'NetworkManager'], timeout=15)
        run_command(['systemctl', 'start', 'wpa_supplicant'], timeout=15)

        logger.info('AP mode stopped, normal WiFi services restored')
        update_lcd_display('AP Stopped', '', ttl=3)
        return True
    except Exception as exc:
        logger.error('Error stopping AP mode: %s', str(exc))
        update_lcd_display('Stop Failed', 'Check logs', ttl=3)
        return False


def configure_wifi(ssid, password):
    """Configure WiFi using nmcli (NetworkManager) so it persists correctly."""
    try:
        logger.info('Configuring WiFi via nmcli for SSID=%s', ssid)

        # Delete any existing connection with this SSID to avoid conflicts
        run_command(['nmcli', 'connection', 'delete', ssid], timeout=10)

        # Add new connection profile
        if password:
            result = run_command([
                'nmcli', 'connection', 'add',
                'type', 'wifi',
                'ifname', 'wlan0',
                'con-name', ssid,
                'ssid', ssid,
                '802-11-wireless-security.key-mgmt', 'wpa-psk',
                '802-11-wireless-security.psk', password,
                'connection.autoconnect', 'yes',
                'connection.autoconnect-priority', '10',
            ], timeout=15)
        else:
            result = run_command([
                'nmcli', 'connection', 'add',
                'type', 'wifi',
                'ifname', 'wlan0',
                'con-name', ssid,
                'ssid', ssid,
                'connection.autoconnect', 'yes',
                'connection.autoconnect-priority', '10',
            ], timeout=15)

        if result and result.returncode == 0:
            logger.info('nmcli connection profile created for SSID=%s', ssid)
            return True
        else:
            err = result.stderr.strip() if result and result.stderr else 'unknown'
            logger.error('nmcli add connection failed: %s', err)
            # Fallback: write wpa_supplicant.conf
            return _configure_wifi_wpa_supplicant(ssid, password)
    except Exception as exc:
        logger.error('Error configuring WiFi via nmcli: %s', str(exc))
        return _configure_wifi_wpa_supplicant(ssid, password)


def _configure_wifi_wpa_supplicant(ssid, password):
    """Fallback: configure WiFi via wpa_supplicant.conf."""
    try:
        config = f'''country=BG
ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1

network={{
    ssid="{ssid}"
    psk="{password}"
}}
'''
        with open('/etc/wpa_supplicant/wpa_supplicant.conf', 'w') as f:
            f.write(config)
        os.chmod('/etc/wpa_supplicant/wpa_supplicant.conf', 0o600)
        logger.info('WiFi configured via wpa_supplicant for SSID=%s', ssid)
        return True
    except Exception as exc:
        logger.error('Error configuring WiFi via wpa_supplicant: %s', str(exc))
        return False


def apply_wifi_switch(ssid, password):
    """Apply WiFi configuration and switch from AP to client mode."""
    try:
        logger.info('Applying WiFi switch to SSID=%s', ssid)
        update_lcd_display('Configuring...', ssid[:16], ttl=3)

        # Stop AP mode
        if not stop_ap_mode():
            logger.error('Failed to stop AP mode')
            return False

        # Configure WiFi
        if not configure_wifi(ssid, password):
            logger.error('Failed to configure WiFi')
            update_lcd_display('Config Failed', 'Check logs', ttl=3)
            return False

        # Connect using the new nmcli profile
        logger.info('Connecting to %s via nmcli...', ssid)
        time.sleep(2)
        result = run_command(['nmcli', 'connection', 'up', ssid], timeout=30)
        if result and result.returncode == 0:
            logger.info('nmcli connection up succeeded for %s', ssid)
        else:
            # Fallback: let NetworkManager auto-connect
            logger.warning('nmcli connection up failed, letting NM auto-connect...')
            run_command(['nmcli', 'dev', 'wifi', 'connect', ssid], timeout=20)

        logger.info('WiFi switch completed successfully')
        update_lcd_display('Connecting...', ssid[:16], ttl=3)
        return True
    except Exception as exc:
        logger.error('Error applying WiFi switch: %s', str(exc))
        update_lcd_display('Switch Failed', 'Check logs', ttl=3)
        return False


def render_page(status=None, status_class='info', ssid=None, password=''):
    """Render the configuration page."""
    pending = read_pending_config()
    current_ssid, ip_address, mode, mode_class, service_status = get_current_wifi_status()
    display_ssid = pending.get('ssid', '') if ssid is None else ssid
    networks = LAST_SCAN_RESULTS

    # Update LCD with current status (via lcd_service)
    if AP_MODE_ACTIVE:
        update_lcd_display('AP Mode', AP_IP, ttl=0)
    elif current_ssid != 'Not connected':
        # In client mode, update briefly when page is loaded
        update_lcd_display(current_ssid[:16], ip_address[:16], ttl=5)

    watchdog_status = 'Enabled ✓' if WATCHDOG_ENABLED else 'Disabled ✗'

    return render_template(
        'wifi_manager.html',
        status=status,
        status_class=status_class,
        ssid=display_ssid,
        password=password,
        ip_address=ip_address,
        current_ssid=current_ssid,
        mode=mode,
        mode_class=mode_class,
        service_status=service_status,
        networks=networks,
        watchdog_enabled=WATCHDOG_ENABLED,
        watchdog_status=watchdog_status,
    )


# ---------------------------------------------------------------------------
# Captive portal – catch-all before_request handler.
# When in AP mode every DNS query resolves to AP_IP (via dnsmasq address=/#/)
# and port 80 is forwarded to AP_PORT via iptables, so any unknown path the
# phone's OS probes (Android /generate_204, iOS /hotspot-detect.html, Windows
# /connecttest.txt, …) lands here and gets redirected to the config page.
# ---------------------------------------------------------------------------
@app.before_request
def captive_portal_redirect():
    """Redirect unknown paths to the config page when in AP mode."""
    if not AP_MODE_ACTIVE:
        return None  # Not in AP mode – don't interfere
    if request.path in KNOWN_ROUTES:
        return None  # Known route – let it proceed normally
    if request.path.startswith('/static/'):
        return None  # Static assets – serve normally
    logger.debug('Captive portal redirect: %s → /', request.path)
    return redirect(f'http://{AP_IP}:{AP_PORT}/', code=302)


@app.route('/')
def index():
    """Main configuration page."""
    return render_page()


@app.route('/scan', methods=['POST'])
def scan_networks():
    """Scan for available WiFi networks."""
    update_lcd_display('Scanning WiFi...', 'Please wait', ttl=3)
    networks = get_available_networks(force_rescan=True)
    if networks:
        update_lcd_display('Scan Complete', f'{len(networks)} networks', ttl=5)
        return render_page(status=f'Found {len(networks)} network(s).', status_class='success')
    update_lcd_display('Scan Complete', 'No networks', ttl=5)
    return render_page(status='No WiFi networks found during scan.', status_class='warning')


@app.route('/select', methods=['POST'])
def select_network():
    """Select a network from the scan results."""
    ssid = request.form.get('ssid', '').strip()
    if not ssid:
        return render_page(status='No SSID was selected.', status_class='error')
    pending = read_pending_config()
    return render_page(status=f'Selected network: {ssid}', status_class='info', ssid=ssid, password=pending.get('password', ''))


@app.route('/save', methods=['POST'])
def save_config():
    """Save WiFi credentials."""
    ssid = request.form.get('ssid', '').strip()
    password = request.form.get('password', '').strip()

    if not ssid:
        update_lcd_display('Error', 'Enter SSID', ttl=3)
        return render_page(status='Please enter a network name.', status_class='error', ssid='')

    update_lcd_display('Saving...', ssid[:16], ttl=3)
    write_pending_config(ssid, password)
    get_available_networks(force_rescan=False)
    update_lcd_display('Saved!', ssid[:16], ttl=5)
    return render_page(
        status=f'Saved WiFi credentials for {ssid}. Use the apply button when ready.',
        status_class='success',
        ssid=ssid,
        password=password,
    )


@app.route('/apply', methods=['POST'])
def apply_saved_wifi():
    """Apply saved WiFi credentials and switch from AP to client mode."""
    pending = read_pending_config()
    ssid = pending.get('ssid', '').strip()
    password = pending.get('password', '')

    if not ssid:
        update_lcd_display('Error', 'No credentials', ttl=3)
        return render_page(status='No saved WiFi credentials found. Save a network first.', status_class='error')

    update_lcd_display('Switching...', ssid[:16], ttl=3)

    # Start the switch in a background thread
    def background_switch():
        success = apply_wifi_switch(ssid, password)
        if success:
            logger.info('WiFi switch completed. Flask server stays running on new IP.')
            update_lcd_display('Connected!', ssid[:16], ttl=3)
            # Wait for network to settle, then update LCD with new IP
            time.sleep(5)
            ip_result = run_command(['hostname', '-I'], timeout=5)
            new_ip = ip_result.stdout.strip().split()[0] if ip_result and ip_result.stdout else '?'
            update_lcd_display(ssid[:16], new_ip, ttl=5)
            logger.info('New IP address: %s', new_ip)
        else:
            logger.error('WiFi switch failed')
            update_lcd_display('Switch Failed', 'Check logs', ttl=3)

    thread = threading.Thread(target=background_switch, daemon=True)
    thread.start()

    return render_page(
        status=(
            f'Started switch to {ssid}. This page will disconnect in a few seconds. '
            'Reconnect the Pi from your normal router after the switch completes.'
        ),
        status_class='warning',
        ssid=ssid,
        password=password,
    )


@app.route('/reset_ap', methods=['POST'])
def reset_to_ap():
    """Reset to AP mode."""
    update_lcd_display('Resetting...', 'To AP mode', ttl=3)

    # Start the reset in a background thread
    def background_reset():
        success, message = start_ap_mode()
        if success:
            logger.info('Reset to AP mode completed')
            update_lcd_display('AP Mode Active', AP_IP, ttl=0)
        else:
            logger.error('Reset to AP mode failed: %s', message)
            update_lcd_display('Reset Failed', 'Check logs', ttl=0)

    thread = threading.Thread(target=background_reset, daemon=True)
    thread.start()

    return render_page(
        status='Resetting to AP mode. Connect to tH-Monitor-Config network.',
        status_class='warning',
    )


@app.route('/watchdog_toggle', methods=['POST'])
def watchdog_toggle():
    """Toggle the WiFi watchdog on/off."""
    global WATCHDOG_ENABLED
    WATCHDOG_ENABLED = not WATCHDOG_ENABLED
    state = 'enabled' if WATCHDOG_ENABLED else 'disabled'
    logger.info('WiFi watchdog %s', state)
    return render_page(
        status=f'Auto-reconnect watchdog {state}.',
        status_class='success' if WATCHDOG_ENABLED else 'warning',
    )


@app.route('/status', methods=['GET'])
def status_api():
    """API endpoint for status."""
    current_ssid, ip_address, mode, mode_class, service_status = get_current_wifi_status()
    return jsonify({
        'ssid': current_ssid,
        'ip': ip_address,
        'mode': mode,
        'mode_class': mode_class,
        'service_status': service_status,
        'ap_active': AP_MODE_ACTIVE,
    })


def lcd_heartbeat():
    """Background thread: refresh the wifi_manager LCD slot periodically.

    In AP mode: refreshes every 30s to keep the slot active indefinitely.
    In client mode: disabled (we want monitor to display by default).
    This ensures wifi_manager content is only shown when user is actively
    accessing the web interface.
    """
    while True:
        time.sleep(30)
        if AP_MODE_ACTIVE:
            update_lcd_display('AP Mode', AP_IP)


def wifi_watchdog():
    """Background thread: monitors WiFi connection and auto-reconnects or falls back to AP."""
    global WATCHDOG_ENABLED
    fail_count = 0
    reconnect_count = 0

    logger.info('WiFi watchdog started (interval=%ds, threshold=%d)', WATCHDOG_INTERVAL, WATCHDOG_FAIL_THRESHOLD)

    while True:
        try:
            time.sleep(WATCHDOG_INTERVAL)

            if not WATCHDOG_ENABLED:
                continue

            # Only watch in client mode
            if AP_MODE_ACTIVE or CURRENT_MODE == 'ap':
                fail_count = 0
                reconnect_count = 0
                continue

            # Check if we have a WiFi connection
            ssid_result = run_command(['iwgetid', '-r'], timeout=5)
            current_ssid = ssid_result.stdout.strip() if ssid_result and ssid_result.stdout else ''

            if current_ssid:
                # Connected - reset counters
                if fail_count > 0:
                    logger.info('Watchdog: WiFi reconnected to %s', current_ssid)
                    ip_result = run_command(['hostname', '-I'], timeout=5)
                    new_ip = ip_result.stdout.strip().split()[0] if ip_result and ip_result.stdout else '?'
                    # Brief display, then monitor resumes
                    update_lcd_display(current_ssid[:16], new_ip, ttl=3)
                fail_count = 0
                reconnect_count = 0
            else:
                fail_count += 1
                logger.warning('Watchdog: WiFi not connected (fail %d/%d)', fail_count, WATCHDOG_FAIL_THRESHOLD)
                # Brief display, then monitor resumes
                update_lcd_display('WiFi Lost!', f'Retry {fail_count}/{WATCHDOG_FAIL_THRESHOLD}', ttl=3)

                if fail_count >= WATCHDOG_FAIL_THRESHOLD:
                    pending = read_pending_config()
                    saved_ssid = pending.get('ssid', '').strip()
                    saved_password = pending.get('password', '')

                    if saved_ssid and reconnect_count < WATCHDOG_RECONNECT_TRIES:
                        # Try to reconnect using saved credentials
                        reconnect_count += 1
                        logger.info('Watchdog: Attempting reconnect to %s (try %d/%d)',
                                    saved_ssid, reconnect_count, WATCHDOG_RECONNECT_TRIES)
                        # Brief display, then monitor resumes
                        update_lcd_display('Reconnecting...', saved_ssid[:16], ttl=3)

                        # Try nmcli connect first
                        result = run_command(['nmcli', 'dev', 'wifi', 'connect', saved_ssid,
                                              'password', saved_password, 'ifname', 'wlan0'], timeout=20)
                        if result and result.returncode == 0:
                            logger.info('Watchdog: Reconnected to %s', saved_ssid)
                            fail_count = 0
                        else:
                            logger.warning('Watchdog: nmcli reconnect failed, trying wpa_supplicant...')
                            run_command(['wpa_cli', '-i', 'wlan0', 'reconnect'], timeout=10)
                            time.sleep(10)
                            # Check again
                            ssid_check = run_command(['iwgetid', '-r'], timeout=5)
                            if ssid_check and ssid_check.stdout.strip():
                                logger.info('Watchdog: Reconnected via wpa_cli')
                                fail_count = 0
                    else:
                        # Too many failures - fall back to AP mode
                        logger.warning('Watchdog: WiFi failed %d times, falling back to AP mode', fail_count)
                        # Brief display before AP mode takes over
                        update_lcd_display('WiFi Failed', 'Starting AP...', ttl=3)
                        fail_count = 0
                        reconnect_count = 0
                        success, message = start_ap_mode()
                        if success:
                            logger.info('Watchdog: Fell back to AP mode successfully')
                        else:
                            logger.error('Watchdog: Failed to start AP mode: %s', message)

        except Exception as exc:
            logger.error('Watchdog error: %s', str(exc))


def signal_handler(sig, frame):
    """Handle shutdown signals."""
    logger.info('Received signal %s, shutting down...', sig)
    update_lcd_display('Shutting down...', '', ttl=3)
    stop_ap_mode()
    sys.exit(0)


def initialize_service():
    """Initialize the WiFi manager service."""
    global CURRENT_MODE, AP_MODE_ACTIVE

    logger.info('=== WiFi Manager Service Starting ===')

    # Note: LCD will be initialized lazily when first used
    # No initialization needed at startup - drivers module handles it

    # Load previous state
    load_state()

    # Show starting message on LCD (briefly, so monitor resumes)
    update_lcd_display('tH-Monitor', 'Starting...', ttl=3)
    time.sleep(1)

    # Determine startup mode
    # Always start AP if:
    #   - No saved WiFi credentials, OR
    #   - Previous mode was 'ap'
    # When we DO have credentials, give NetworkManager up to 30 s to connect
    # before concluding WiFi is unavailable and starting AP mode.  This avoids
    # the race at boot where wifi-manager starts before NM has connected.
    pending = read_pending_config()
    current_ssid, _, _, _, _ = get_current_wifi_status()

    has_credentials = bool(pending.get('ssid'))
    must_start_ap = not has_credentials or CURRENT_MODE == 'ap'

    if not must_start_ap and current_ssid == 'Not connected':
        # Wait for NetworkManager to establish the saved connection
        logger.info('No WiFi yet – waiting up to 30 s for NetworkManager…')
        update_lcd_display('Waiting for WiFi', pending.get('ssid', '')[:16], ttl=10)
        for wait_n in range(30):
            time.sleep(1)
            ssid_result = run_command(['iwgetid', '-r'], timeout=5)
            current_ssid = ssid_result.stdout.strip() if ssid_result and ssid_result.stdout else ''
            if current_ssid:
                logger.info('WiFi connected to %s after %d s', current_ssid, wait_n + 1)
                break
        else:
            logger.warning('WiFi did not connect within 30 s – starting AP mode')
        current_ssid = current_ssid or 'Not connected'

    should_start_ap = must_start_ap or current_ssid == 'Not connected'

    if should_start_ap:
        logger.info('Starting in AP mode')
        success, message = start_ap_mode()
        if not success:
            logger.error('Failed to start AP mode: %s', message)
            # Don't exit - stay alive for recovery via SSH
            # Show error on LCD but keep service running
            update_lcd_display('AP Failed', 'SSH to recover', ttl=0)
            print(f'WARNING: {message}')
            print('Service will stay alive for recovery via SSH.')
            print('Connect via SSH and run: sudo systemctl restart wifi-manager.service')
    else:
        logger.info('Starting in client mode (connected to %s)', current_ssid)
        update_lcd_display(current_ssid[:16], 'Client Mode', ttl=5)

    # Get initial network scan
    get_available_networks(force_rescan=False)

    # Start WiFi watchdog thread
    watchdog_thread = threading.Thread(target=wifi_watchdog, daemon=True, name='wifi-watchdog')
    watchdog_thread.start()
    logger.info('WiFi watchdog thread started')

    # Start LCD heartbeat thread (refreshes AP-mode slot every 30 s)
    heartbeat_thread = threading.Thread(target=lcd_heartbeat, daemon=True, name='lcd-heartbeat')
    heartbeat_thread.start()
    logger.info('LCD heartbeat thread started')

    logger.info('WiFi Manager Service initialized')


if __name__ == '__main__':
    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Initialize service
    initialize_service()

    # Start Flask web server
    logger.info('Starting web server on http://%s:%d', AP_IP, AP_PORT)
    try:
        app.run(host='0.0.0.0', port=AP_PORT, debug=False, threaded=True)
    except Exception as exc:
        logger.error('Web server error: %s', str(exc))
        stop_ap_mode()
        sys.exit(1)
