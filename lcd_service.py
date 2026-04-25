#!/usr/bin/env python3
"""
LCD Display Service for tH-Monitor
===================================
Centralized daemon that owns the I2C LCD exclusively and serves display
requests from multiple client processes via a Unix domain socket.

Protocol  – newline-delimited JSON on /tmp/lcd.sock

Write request:
  {"client": "wifi_manager", "priority": 10, "line1": "AP Mode",
   "line2": "192.168.4.1", "ttl": 0}

Clear request (release a client's slot):
  {"client": "wifi_manager", "action": "clear"}

Priority convention (lower number = higher importance):
  10  – wifi_manager  (AP mode, critical WiFi status)
  20  – system alerts
  50  – monitor       (sensor readings, clock)
  90  – demo scripts / one-off messages
"""

import json
import logging
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path

# ── LCD driver (RPLCD – modern Pi-compatible library) ────────────────────────
# Install: pip3 install RPLCD smbus2
#
# RPLCD wraps the HD44780 controller via the PCF8574 I2C expander that most
# cheap 16x2 LCD backpacks use.  It uses smbus2 (pure-Python, Pi 5-compatible)
# and has no RPi.GPIO dependency in I2C mode.
#
# Default I2C address for PCF8574-based backpacks is 0x27.
# Change LCD_I2C_ADDRESS below if yours uses 0x3F or another address.

LCD_I2C_ADDRESS = 0x27   # PCF8574 expander default; change to 0x3F if needed
LCD_I2C_PORT    = 1      # I2C bus 1 on all modern Raspberry Pi models
LCD_COLS        = 16
LCD_ROWS        = 2

try:
    from RPLCD.i2c import CharLCD as _CharLCD
    _RPLCD_AVAILABLE = True
except ImportError as _e:
    _RPLCD_AVAILABLE = False
    print(f'Warning: RPLCD not available: {_e}. Install with: pip3 install RPLCD smbus2')

# ── Configuration ─────────────────────────────────────────────────────────────

SOCKET_PATH = '/tmp/lcd.sock'
LOG_FILE    = Path('/home/raspberry/tH-monitor/lcd_service.log')
RENDER_INTERVAL   = 0.5   # seconds between LCD refresh cycles
STALE_THRESHOLD   = 60    # seconds: evict client slot if no update received
MAX_LINE_LEN      = 16    # 2×16 LCD

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format='%(asctime)s %(levelname)s [lcd_service] %(message)s',
)
logger = logging.getLogger(__name__)

# ── Shared state ──────────────────────────────────────────────────────────────

# Slot dict: { client_name: { priority, line1, line2, expires_at, updated_at } }
# expires_at = None  → indefinite (never auto-evicted; removed only by explicit 'clear')
# expires_at = float → absolute time.time() deadline for TTL-limited messages
_slots: dict = {}
_slots_lock = threading.Lock()

# ── LCD helpers ───────────────────────────────────────────────────────────────

_display: '_CharLCD | None' = None
_display_lock = threading.Lock()


def _init_lcd() -> bool:
    """Attempt to initialise the LCD via RPLCD.  Returns True on success."""
    global _display
    if not _RPLCD_AVAILABLE:
        return False
    try:
        if _display is not None:
            try:
                _display.close(clear=True)
            except Exception:
                pass
        _display = _CharLCD(
            i2c_expander='PCF8574',
            address=LCD_I2C_ADDRESS,
            port=LCD_I2C_PORT,
            cols=LCD_COLS,
            rows=LCD_ROWS,
            auto_linebreaks=False,
            backlight_enabled=True,
        )
        logger.info('LCD initialized via RPLCD at I2C address 0x%02X', LCD_I2C_ADDRESS)
        return True
    except Exception as exc:
        logger.error('LCD init failed: %s', exc)
        _display = None
        return False


def _lcd_write(line1: str, line2: str) -> None:
    """Write two lines to the LCD, reinitialising on I2C errors."""
    global _display
    if not _RPLCD_AVAILABLE:
        return

    # Pad / truncate to exactly LCD_COLS chars so RPLCD never wraps
    line1 = line1[:LCD_COLS].ljust(LCD_COLS)
    line2 = line2[:LCD_COLS].ljust(LCD_COLS)

    with _display_lock:
        for attempt in range(3):
            try:
                if _display is None:
                    if not _init_lcd():
                        return
                _display.cursor_pos = (0, 0)
                _display.write_string(line1)
                _display.cursor_pos = (1, 0)
                _display.write_string(line2)
                return
            except Exception as exc:
                logger.warning('LCD write error (attempt %d): %s', attempt + 1, exc)
                _display = None
                time.sleep(0.5)


def _lcd_clear() -> None:
    """Clear the LCD display."""
    global _display
    if not _RPLCD_AVAILABLE:
        return
    with _display_lock:
        try:
            if _display is None:
                _init_lcd()
            if _display:
                _display.clear()
        except Exception as exc:
            logger.warning('LCD clear error: %s', exc)
            _display = None


# ── Render loop ───────────────────────────────────────────────────────────────

def _render_loop() -> None:
    """Background thread: pick highest-priority slot and write to LCD."""
    _previous_key = (None, None)   # (line1, line2) of last written content

    while True:
        try:
            now = time.time()
            best = None

            with _slots_lock:
                # Evict only TTL-limited entries that have expired.
                # Indefinite slots (expires_at=None) are kept until the client
                # explicitly sends an 'action': 'clear' message – this ensures
                # high-priority slots (e.g. wifi_manager AP mode) aren't silently
                # removed just because the client hasn't written recently.
                to_remove = [
                    name for name, slot in _slots.items()
                    if slot['expires_at'] is not None and now >= slot['expires_at']
                ]
                for name in to_remove:
                    logger.debug('Evicting expired TTL slot: %s', name)
                    del _slots[name]

                # Pick highest priority (lowest number) active slot
                active = {n: s for n, s in _slots.items()
                          if s['expires_at'] is None or now < s['expires_at']}
                if active:
                    best_name = min(active, key=lambda n: (active[n]['priority'], n))
                    best = active[best_name]

            if best:
                key = (best['line1'], best['line2'])
                if key != _previous_key:
                    _lcd_write(best['line1'], best['line2'])
                    _previous_key = key
            elif _previous_key != ('', ''):
                _lcd_clear()
                _previous_key = ('', '')

        except Exception as exc:
            logger.error('Render loop error: %s', exc)

        time.sleep(RENDER_INTERVAL)


# ── Socket handler ────────────────────────────────────────────────────────────

def _handle_client(conn: socket.socket, addr) -> None:
    """Handle a single client connection (runs in its own thread)."""
    try:
        buf = b''
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk
            # Process all complete (newline-terminated) messages
            while b'\n' in buf:
                line, buf = buf.split(b'\n', 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line.decode('utf-8'))
                except json.JSONDecodeError as exc:
                    logger.warning('Bad JSON from client: %s — %s', line[:80], exc)
                    continue

                client  = msg.get('client', 'unknown')
                action  = msg.get('action', 'write')

                if action == 'clear':
                    with _slots_lock:
                        _slots.pop(client, None)
                    logger.debug('Cleared slot for client: %s', client)
                elif action == 'write':
                    priority   = int(msg.get('priority', 50))
                    line1      = str(msg.get('line1', ''))[:MAX_LINE_LEN]
                    line2      = str(msg.get('line2', ''))[:MAX_LINE_LEN]
                    ttl        = int(msg.get('ttl', 0))
                    expires_at = (time.time() + ttl) if ttl > 0 else None

                    with _slots_lock:
                        _slots[client] = {
                            'priority':   priority,
                            'line1':      line1,
                            'line2':      line2,
                            'expires_at': expires_at,
                            'updated_at': time.time(),
                        }
                    logger.debug('Slot updated: client=%s pri=%d l1=%r l2=%r',
                                 client, priority, line1, line2)
    except Exception as exc:
        logger.error('Client handler error: %s', exc)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _server_loop() -> None:
    """Listen for incoming client connections."""
    # Remove leftover socket from previous run
    try:
        os.unlink(SOCKET_PATH)
    except FileNotFoundError:
        pass

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(SOCKET_PATH)
    os.chmod(SOCKET_PATH, 0o666)   # allow non-root clients
    sock.listen(10)
    logger.info('LCD service listening on %s', SOCKET_PATH)

    while True:
        try:
            conn, addr = sock.accept()
            t = threading.Thread(target=_handle_client, args=(conn, addr),
                                 daemon=True, name='lcd-client')
            t.start()
        except Exception as exc:
            logger.error('Server accept error: %s', exc)
            time.sleep(1)


# ── Signal handling / shutdown ────────────────────────────────────────────────

def _shutdown(sig, frame) -> None:
    logger.info('Received signal %s, shutting down…', sig)
    _lcd_clear()
    try:
        os.unlink(SOCKET_PATH)
    except Exception:
        pass
    sys.exit(0)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    logger.info('=== LCD Service Starting (RPLCD driver) ===')

    # Make sure the log directory exists
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Initialise LCD hardware
    if not _RPLCD_AVAILABLE:
        logger.warning('RPLCD not installed – LCD writes will be no-ops. '
                       'Install with: pip3 install RPLCD smbus2')
    _init_lcd()

    # Start render thread
    render_thread = threading.Thread(target=_render_loop, daemon=True,
                                     name='lcd-render')
    render_thread.start()
    logger.info('Render loop thread started (interval=%.1fs)', RENDER_INTERVAL)

    # Run socket server in main thread (blocks until shutdown)
    _server_loop()
