#!/usr/bin/env python3
"""
WiFi Configuration Web Server
Based on RaspiWiFi concept - provides a web interface to configure WiFi credentials
Runs on port 8080 and creates an AP mode when in configuration mode
"""

from flask import Flask, render_template_string, request, redirect, url_for
import subprocess
import os
import time
import logging
import threading

app = Flask(__name__)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# WiFi configuration storage
WIFI_CONFIG_FILE = '/etc/wpa_supplicant/wpa_supplicant.conf'

# HTML Template for the configuration page
HTML_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <title>tH-Monitor WiFi Config</title>
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
        .status.info {
            background-color: #d1ecf1;
            color: #0c5460;
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
        .networks {
            margin: 20px 0;
        }
        .network-item {
            padding: 10px;
            margin: 5px 0;
            background: #f9f9f9;
            border-radius: 5px;
            cursor: pointer;
        }
        .network-item:hover {
            background: #e9e9e9;
        }
        .current-status {
            text-align: center;
            margin-bottom: 20px;
            padding: 10px;
            background: #e3f2fd;
            border-radius: 5px;
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
        
        <form method="POST" action="/save">
            <label for="ssid">WiFi Network Name (SSID):</label>
            <input type="text" id="ssid" name="ssid" value="{{ ssid }}" required>
            
            <label for="password">WiFi Password:</label>
            <input type="password" id="password" name="password" placeholder="Enter password">
            
            <button type="submit">Save & Connect</button>
        </form>
        
        <form method="POST" action="/reset">
            <button type="submit" style="background-color: #dc3545;">Reset to Configuration Mode</button>
        </form>
    </div>
</body>
</html>
'''

def get_current_wifi_status():
    """Get current WiFi status"""
    try:
        result = subprocess.run(['hostname', '-I'], capture_output=True, text=True, timeout=5)
        ip_address = result.stdout.strip().split()[0] if result.stdout else 'Not connected'
    except:
        ip_address = 'Not connected'
    
    try:
        result = subprocess.run(['iwgetid', '-r'], capture_output=True, text=True, timeout=5)
        ssid = result.stdout.strip() if result.stdout else 'Not connected'
    except:
        ssid = 'Not connected'
    
    return ssid, ip_address

def get_available_networks():
    """Scan for available WiFi networks"""
    try:
        result = subprocess.run(['sudo', 'iwlist', 'wlan0', 'scan'], capture_output=True, text=True, timeout=10)
        lines = result.stdout.split('\n')
        networks = []
        current_ssid = ''
        
        for line in lines:
            if 'ESSID' in line:
                ssid = line.split('"')[1] if '"' in line else ''
                if ssid and ssid not in networks:
                    networks.append(ssid)
        
        return networks[:10]  # Return first 10 networks
    except:
        return []

def configure_wifi(ssid, password):
    """Configure WiFi with new credentials"""
    try:
        # Create wpa_supplicant configuration
        config = f'''country=US
ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1

network={{
    ssid="{ssid}"
    psk="{password}"
}}
'''
        
        # Write to wpa_supplicant.conf
        with open(WIFI_CONFIG_FILE, 'w') as f:
            f.write(config)
        
        # Make sure only root can read the password
        os.chmod(WIFI_CONFIG_FILE, 0o600)
        
        # Restart wpa_supplicant
        subprocess.run(['sudo', 'wpa_cli', '-i', 'wlan0', 'reconfigure'], timeout=10)
        
        return True, "WiFi configured! Rebooting to connect..."
    except Exception as e:
        return False, f"Error configuring WiFi: {str(e)}"

def restart_network():
    """Restart network services"""
    try:
        subprocess.run(['sudo', 'systemctl', 'restart', 'dhcpcd'], timeout=10)
        return True
    except:
        try:
            subprocess.run(['sudo', 'systemctl', 'restart', 'NetworkManager'], timeout=10)
            return True
        except:
            return False

@app.route('/')
def index():
    """Main configuration page"""
    ssid, ip_address = get_current_wifi_status()
    return render_template_string(HTML_TEMPLATE, 
                                  status=None, 
                                  status_class='info',
                                  ssid=ssid,
                                  ip_address=ip_address,
                                  current_ssid=ssid)

@app.route('/save', methods=['POST'])
def save_config():
    """Save WiFi configuration"""
    ssid = request.form.get('ssid', '').strip()
    password = request.form.get('password', '').strip()
    
    if not ssid:
        ssid, ip_address = get_current_wifi_status()
        return render_template_string(HTML_TEMPLATE,
                                      status="Please enter a network name",
                                      status_class='error',
                                      ssid='',
                                      ip_address=ip_address,
                                      current_ssid=ssid)
    
    success, message = configure_wifi(ssid, password)
    
    ssid, ip_address = get_current_wifi_status()
    
    if success:
        # Schedule a reboot after a short delay
        def delayed_reboot():
            time.sleep(3)
            subprocess.run(['sudo', 'reboot'])
        
        thread = threading.Thread(target=delayed_reboot)
        thread.daemon = True
        thread.start()
    
    return render_template_string(HTML_TEMPLATE,
                                  status=message,
                                  status_class='success' if success else 'error',
                                  ssid=ssid,
                                  ip_address=ip_address,
                                  current_ssid=ssid)

@app.route('/reset', methods=['POST'])
def reset_mode():
    """Reset to configuration mode (disconnect current WiFi)"""
    try:
        # Remove existing network configurations
        subprocess.run(['sudo', 'wpa_cli', '-i', 'wlan0', 'remove_network', '0'], timeout=5)
        subprocess.run(['sudo', 'wpa_cli', '-i', 'wlan0', 'save_config'], timeout=5)
        
        # Create an open network for configuration
        config = '''country=US
ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1
'''
        with open(WIFI_CONFIG_FILE, 'w') as f:
            f.write(config)
        
        return render_template_string(HTML_TEMPLATE,
                                      status="Network reset. Device is now in configuration mode.",
                                      status_class='info',
                                      ssid='',
                                      ip_address='Not connected',
                                      current_ssid='Not connected')
    except Exception as e:
        return render_template_string(HTML_TEMPLATE,
                                      status=f"Error: {str(e)}",
                                      status_class='error',
                                      ssid='',
                                      ip_address='Not connected',
                                      current_ssid='Not connected')

if __name__ == '__main__':
    logger.info("Starting WiFi Configuration Server on port 8080...")
    logger.info("Access at: http://<raspberry-pi-ip>:8080")
    
    # Run Flask app
    app.run(host='0.0.0.0', port=8080, debug=False, threaded=True)
