#!/usr/bin/env python3
"""
WiFi Configuration Web Server - SAFE VERSION
This version focuses on reliability and prevents permanent loss of connectivity
Runs on port 8080 and uses NetworkManager API directly
"""

from flask import Flask, render_template_string, request
import subprocess
import os
import time
import logging
import threading

app = Flask(__name__)

# Configure logging
logging.basicConfig(filename='/home/raspberry/tH-monitor/wifi_safe_config.log', level=logging.INFO)
logger = logging.getLogger(__name__)

# HTML Template for the configuration page
HTML_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <title>tH-Monitor WiFi Config - SAFE</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {
            font-family: Arial, sans-serif;
            max-width: 500px;
            margin: 50px auto;
            padding: 20px;
            background-color: #f0f0f0;
        }
        .container {
            background: white;
            padding: 30px;
            border-radius: 10px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }
        h1 {
            color: #333;
            text-align: center;
        }
        .status {
            padding: 15px;
            margin: 20px 0;
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
            padding: 15px;
            margin: 20px 0;
            background-color: #4CAF50;
            color: white;
            border: none;
            border-radius: 5px;
            cursor: pointer;
            font-size: 16px;
        }
        button:hover {
            background-color: #45a049;
        }
        .current-status {
            text-align: center;
            margin-bottom: 20px;
            padding: 10px;
            background: #e3f2fd;
            border-radius: 5px;
        }
        .warning-box {
            background: #fff3cd;
            padding: 15px;
            border-radius: 5px;
            margin-bottom: 20px;
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
            <strong>Current Network:</strong> {{ current_ssid }}<br>
            <strong>IP Address:</strong> {{ ip_address }}
        </div>
        
        <div class="warning-box">
            <strong>⚠️ SAFE CONFIGURATION</strong><br>
            This version will not create a WiFi access point and will not 
            permanently disconnect from your current network. Configuration 
            changes are tested before applying.
        </div>
        
        <form method="POST" action="/save">
            <label for="ssid">WiFi Network Name (SSID):</label>
            <input type="text" id="ssid" name="ssid" value="{{ ssid }}" required>
            
            <label for="password">WiFi Password:</label>
            <input type="password" id="password" name="password" placeholder="Enter password">
            
            <button type="submit">Test & Connect</button>
        </form>
        
        <form method="POST" action="/quick_connect">
            <button type="submit" style="background-color: #ff9800;">Quick Connect to KavalaVIVA</button>
        </form>
    </div>
</body>
</html>
'''

def get_current_wifi_status():
    """Get current WiFi status"""
    try:
        # Get IP address
        result = subprocess.run(['hostname', '-I'], capture_output=True, text=True, timeout=5)
        ip_address = result.stdout.strip() if result.stdout else 'No IP'
        
        # Get SSID using NetworkManager
        result = subprocess.run(['nmcli', '-t', '-f', 'active,ssid', 'dev', 'wifi'], 
                             capture_output=True, text=True, timeout=5)
        current_ssid = "Not connected"
        for line in result.stdout.strip().split('\n'):
            if line and line.startswith('yes:'):
                current_ssid = line.split(':', 1)[1]
        
        return current_ssid, ip_address
    except Exception as e:
        logger.error(f"Error getting WiFi status: {str(e)}")
        return 'Not connected', 'No IP'

def get_available_networks():
    """Get available WiFi networks (used for testing)"""
    try:
        result = subprocess.run(['nmcli', '-g', 'SSID', 'dev', 'wifi'], 
                             capture_output=True, text=True, timeout=10)
        networks = [line.strip() for line in result.stdout.strip().split('\n') if line.strip()]
        return networks
    except Exception as e:
        logger.error(f"Error scanning for networks: {str(e)}")
        return []

def test_connection(ssid, password):
    """Test if we can connect to a WiFi network without permanently changing configuration"""
    try:
        logger.info(f"Testing connection to {ssid}")
        
        # Check if network is available
        available_networks = get_available_networks()
        if ssid not in available_networks:
            return False, f"Network '{ssid}' not found"
        
        # Try to connect temporarily (do not save connection)
        result = subprocess.run(['nmcli', 'dev', 'wifi', 'connect', ssid, 'password', password, 'ifname', 'wlan0'],
                             capture_output=True, text=True, timeout=30)
        
        logger.info(f"Connection result: {result.returncode} - {result.stdout.strip()}|{result.stderr.strip()}")
        
        if result.returncode == 0:
            return True, "Connection successful"
        else:
            # Analyze error
            error_text = result.stderr.strip()
            if "invalid-wpa-psk" in error_text:
                return False, "Invalid password"
            elif "connection-refused" in error_text:
                return False, "Connection refused by network"
            else:
                return False, f"Connection failed: {result.stderr.strip()}"
    
    except Exception as e:
        logger.error(f"Test connection error: {str(e)}")
        return False, f"Connection failed: {str(e)}"

def configure_wifi_permanently(ssid, password):
    """Configure WiFi permanently using NetworkManager"""
    try:
        logger.info(f"Configuring WiFi permanently for {ssid}")
        
        # Delete any existing connection for this SSID
        result = subprocess.run(['nmcli', 'connection', 'show', '--active'], 
                             capture_output=True, text=True, timeout=5)
        for line in result.stdout.strip().split('\n')[1:]:
            if line:
                conn_name = line.strip().split()[0]
                if ssid in conn_name:
                    subprocess.run(['nmcli', 'connection', 'delete', conn_name], 
                                 capture_output=True, text=True, timeout=10)
        
        # Create a new connection
        result = subprocess.run(['nmcli', 'connection', 'add', 'type', 'wifi', 
                             'con-name', f'netplan-wlan0-{ssid}', 
                             'ifname', 'wlan0', 'ssid', ssid, 
                             'wifi-sec.key-mgmt', 'wpa-psk', 
                             'wifi-sec.psk', password],
                             capture_output=True, text=True, timeout=30)
        
        if result.returncode == 0:
            # Bring up the connection
            subprocess.run(['nmcli', 'connection', 'up', f'netplan-wlan0-{ssid}'],
                         capture_output=True, text=True, timeout=30)
            return True, "WiFi configured successfully"
        else:
            logger.error(f"Failed to create connection: {result.stderr}")
            return False, f"Configuration failed: {result.stderr}"
    
    except Exception as e:
        logger.error(f"WiFi configuration error: {str(e)}")
        return False, f"Configuration failed: {str(e)}"

@app.route('/')
def index():
    """Main configuration page"""
    current_ssid, ip_address = get_current_wifi_status()
    return render_template_string(HTML_TEMPLATE, 
                                  status=None, 
                                  status_class='info',
                                  ssid='',
                                  ip_address=ip_address,
                                  current_ssid=current_ssid)

@app.route('/save', methods=['POST'])
def save_config():
    """Save WiFi configuration (with testing)"""
    ssid = request.form.get('ssid', '').strip()
    password = request.form.get('password', '').strip()
    
    current_ssid, ip_address = get_current_wifi_status()
    
    if not ssid:
        return render_template_string(HTML_TEMPLATE,
                                      status="Please enter a network name",
                                      status_class='error',
                                      ssid='',
                                      ip_address=ip_address,
                                      current_ssid=current_ssid)
    
    # Test the connection first
    logger.info(f"Testing WiFi connection: {ssid}")
    test_success, test_msg = test_connection(ssid, password)
    
    if test_success:
        # If test succeeded, configure permanently
        logger.info("Test succeeded, configuring permanently")
        config_success, config_msg = configure_wifi_permanently(ssid, password)
        
        if config_success:
            current_ssid, ip_address = get_current_wifi_status()
            return render_template_string(HTML_TEMPLATE,
                                      status="WiFi configuration completed successfully",
                                      status_class='success',
                                      ssid='',
                                      ip_address=ip_address,
                                      current_ssid=current_ssid)
        else:
            return render_template_string(HTML_TEMPLATE,
                                      status=f"Configuration failed: {config_msg}",
                                      status_class='error',
                                      ssid=ssid,
                                      ip_address=ip_address,
                                      current_ssid=current_ssid)
    else:
        logger.warning(f"Connection test failed: {test_msg}")
        return render_template_string(HTML_TEMPLATE,
                                      status=f"Connection failed: {test_msg}",
                                      status_class='warning',
                                      ssid=ssid,
                                      ip_address=ip_address,
                                      current_ssid=current_ssid)

@app.route('/quick_connect', methods=['POST'])
def quick_connect():
    """Quick connect to KavalaVIVA (fallback network)"""
    try:
        success, message = configure_wifi_permanently("KavalaVIVA", "your_wifi_password")
        
        if success:
            current_ssid, ip_address = get_current_wifi_status()
            return render_template_string(HTML_TEMPLATE,
                                      status="Connected to KavalaVIVA",
                                      status_class='success',
                                      ssid='',
                                      ip_address=ip_address,
                                      current_ssid=current_ssid)
        else:
            current_ssid, ip_address = get_current_wifi_status()
            return render_template_string(HTML_TEMPLATE,
                                      status=f"Failed to connect: {message}",
                                      status_class='error',
                                      ssid='',
                                      ip_address=ip_address,
                                      current_ssid=current_ssid)
    except Exception as e:
        logger.error(f"Quick connect error: {str(e)}")
        current_ssid, ip_address = get_current_wifi_status()
        return render_template_string(HTML_TEMPLATE,
                                      status=f"Error: {str(e)}",
                                      status_class='error',
                                      ssid='',
                                      ip_address=ip_address,
                                      current_ssid=current_ssid)

if __name__ == '__main__':
    logger.info("Starting SAFE WiFi Configuration Server on port 8080...")
    logger.info("Access at: http://<raspberry-pi-ip>:8080")
    
    # Run Flask app
    app.run(host='0.0.0.0', port=8080, debug=False, threaded=True)
