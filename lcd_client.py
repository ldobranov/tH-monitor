#!/usr/bin/env python3
"""
LCD Client Library for tH-Monitor
===================================
Lightweight importable helper that sends display requests to lcd_service.py
via a Unix domain socket.  Thread-safe and non-blocking – if the service is
unavailable a warning is logged and the caller is never blocked.

Usage:
    from lcd_client import LcdClient

    lcd = LcdClient('monitor')                              # default priority 50
    lcd.write('T1:25.3 T2:24.8', 'H1:65% H2:70%')
    lcd.write('AP Mode', '192.168.4.1', priority=10)
    lcd.write('Scanning...', 'wait', priority=10, ttl=5)   # auto-expire in 5 s
    lcd.clear()                                             # release slot

Priority convention (lower = higher importance):
    10  wifi_manager  – AP mode / WiFi config
    20  system alerts
    50  monitor       – default
    90  demo / misc
"""

import json
import logging
import socket
import threading
import time

SOCKET_PATH = '/tmp/lcd.sock'
CONNECT_TIMEOUT = 2.0   # seconds
MAX_LINE_LEN = 16

logger = logging.getLogger(__name__)


class LcdClient:
    """Thread-safe client for the LCD display service."""

    def __init__(self, client_name: str, default_priority: int = 50) -> None:
        self._name     = client_name
        self._priority = default_priority
        self._sock: socket.socket | None = None
        self._lock     = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────────

    def write(self,
              line1: str,
              line2: str = '',
              priority: int | None = None,
              ttl: int = 0) -> None:
        """Send a display update to the LCD service.

        Args:
            line1:    Top line (max 16 chars).
            line2:    Bottom line (max 16 chars).
            priority: Override default priority for this message.
            ttl:      Auto-expire after this many seconds (0 = indefinite).
        """
        msg = {
            'client':   self._name,
            'action':   'write',
            'priority': priority if priority is not None else self._priority,
            'line1':    str(line1)[:MAX_LINE_LEN],
            'line2':    str(line2)[:MAX_LINE_LEN],
            'ttl':      int(ttl),
        }
        self._send(msg)

    def clear(self) -> None:
        """Release this client's display slot."""
        self._send({'client': self._name, 'action': 'clear'})

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _send(self, msg: dict) -> None:
        """Encode and send a JSON message, reconnecting if needed."""
        payload = (json.dumps(msg) + '\n').encode('utf-8')
        with self._lock:
            for attempt in range(2):
                try:
                    if self._sock is None:
                        self._connect()
                    if self._sock is None:
                        return   # service unavailable
                    self._sock.sendall(payload)
                    return
                except (BrokenPipeError, ConnectionResetError, OSError):
                    logger.debug('LCD socket lost, reconnecting…')
                    self._close()
                    if attempt == 0:
                        time.sleep(0.1)
            logger.warning('Failed to send to LCD service after reconnect attempt')

    def _connect(self) -> None:
        """Open a connection to the socket.  Sets self._sock or leaves it None."""
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(CONNECT_TIMEOUT)
            s.connect(SOCKET_PATH)
            s.settimeout(None)   # switch to blocking for sendall
            self._sock = s
            logger.debug('Connected to LCD service (%s)', self._name)
        except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
            logger.warning('LCD service unavailable (%s): %s', self._name, exc)
            self._sock = None

    def _close(self) -> None:
        """Close the socket quietly."""
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def __del__(self) -> None:
        self._close()
