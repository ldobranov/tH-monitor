#! /usr/bin/env python3
import logging
import drivers
import datetime
import os
import time
import subprocess
import signal
import sys
from pigpio_dht import DHT22
from influxdb import client as influxdb
import pigpio
import threading

#logmode = logging.DEBUG
logmode = logging.WARNING

logging.basicConfig(filename="/home/raspberry/tH-monitor/log_monitor.txt", level=logmode)

# Configuration
BUTTON_PIN = 18  # GPIO pin for button
SENSOR1_PIN = 17
SENSOR2_PIN = 27

sensor1 = DHT22(SENSOR1_PIN)
sensor2 = DHT22(SENSOR2_PIN)

#InfluxDB Connection Details
influxHost = 'localhost'
influxUser = 'admin'
with open(os.path.dirname(os.path.abspath(__file__)) + '/secretstring', 'r') as f:
    influxPasswd = f.readline().strip()

old_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5)

# Thread-safe sensor data storage
sensor_lock = threading.Lock()
tmp1 = None
hum1 = None
tmp2 = None
hum2 = None

# Display modes
DISPLAY_MODES = ['sensors', 'clock', 'sensor1', 'sensor2', 'wifi']
current_mode = 0  # Start with sensor display
wifi_config_active = False  # Track if WiFi config mode is active

# Lock for thread-safe display updates
display_lock = threading.Lock()

# Flag for graceful shutdown
running = True

# InfluxDB client (reusable connection)
influx_client = None

try:
    display = drivers.Lcd()
    logging.warning(datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S') + "  -- LCD started")
except:
    logging.error(datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S') + "  -- LCD error")
    display = None
    pass

# Initialize pigpio for button with callback
try:
    pi = pigpio.pi()
    # Set up button pin with pull-down resistor (button connects to +3.3V)
    pi.set_mode(BUTTON_PIN, pigpio.INPUT)
    pi.set_pull_up_down(BUTTON_PIN, pigpio.PUD_DOWN)
    logging.warning(datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S') + "  -- Button initialized on GPIO" + str(BUTTON_PIN))
    
    # Track last button press time for debouncing
    last_button_press_time = 0
    button_debounce_ms = 300  # 300ms debounce time
    
    # Button press tracking for long press detection
    button_pressed_time = None
    LONG_PRESS_DURATION_MS = 10000  # 10 seconds long press
    
    # Button callback function - triggered on EITHER edge (press or release)
    def button_callback(gpio, level, tick):
        global current_mode, last_button_press_time, button_pressed_time, wifi_config_active
        
        current_time_ms = int(time.time() * 1000)
        
        if level == 1:  # Rising edge = button pressed
            # Button just pressed - start tracking
            button_pressed_time = current_time_ms
            
        elif level == 0 and button_pressed_time is not None:  # Falling edge = button released
            # Button released - calculate press duration
            press_duration = current_time_ms - button_pressed_time
            button_pressed_time = None
            
            # Debounce: ignore if pressed too soon after last press
            if current_time_ms - last_button_press_time < button_debounce_ms:
                return
            
            # Check if long press (10 seconds)
            if press_duration >= LONG_PRESS_DURATION_MS:
                # Long press - enter WiFi config mode
                last_button_press_time = current_time_ms
                logging.info(datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S') + "  -- LONG BUTTON PRESS - Starting WiFi config mode")
                enter_wifi_config_mode()
            else:
                # Short press - cycle display modes
                last_button_press_time = current_time_ms
                # If WiFi config mode is active, stay on wifi mode
                if wifi_config_active:
                    current_mode = len(DISPLAY_MODES) - 1
                else:
                    current_mode = (current_mode + 1) % len(DISPLAY_MODES)
                logging.info(datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S') + "  -- Button pressed, mode: " + DISPLAY_MODES[current_mode])
                update_display()
    
    # Set up callback on BOTH edges (rising and falling)
    pi.callback(BUTTON_PIN, pigpio.EITHER_EDGE, button_callback)
    
except Exception as e:
    pi = None
    logging.error(datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S') + "  -- Button initialization failed: " + str(e))

def get_wifi_status():
    """Get WiFi status and IP address"""
    try:
        # Get IP address
        result = subprocess.run(['hostname', '-I'], capture_output=True, text=True, timeout=5)
        ip_address = result.stdout.strip() if result.stdout else 'No IP'
        
        # Check if connected to network
        result = subprocess.run(['ping', '-c', '1', '-W', '2', '8.8.8.8'], capture_output=True, text=True)
        wifi_status = 'Online' if result.returncode == 0 else 'Offline'
        
        return ip_address, wifi_status
    except Exception as e:
        logging.error(datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S') + "  -- WiFi status error: " + str(e))
        return 'Error', 'Unknown'

def enter_wifi_config_mode():
    """Enter WiFi configuration mode - shows instructions on LCD"""
    global current_mode, wifi_config_active
    
    wifi_config_active = True
    
    # Change to wifi mode (will show config info)
    current_mode = len(DISPLAY_MODES) - 1  # Last mode is wifi
    
    if display is not None:
        try:
            display.lcd_display_string("WiFi Config", 1)
            display.lcd_display_string("Starting...", 2)
            time.sleep(2)
        except:
            pass
    
    # Start WiFi config service
    try:
        subprocess.run(['sudo', 'systemctl', 'start', 'wifi_config.service'], timeout=10)
        logging.info("WiFi config service started")
    except Exception as e:
        logging.error("Failed to start WiFi config service: " + str(e))
    
    # Update display to show WiFi config info
    update_display()

def update_display():
    """Update LCD based on current display mode - thread safe"""
    if display is None:
        return
    
    # Get current sensor values in a thread-safe manner
    with sensor_lock:
        local_tmp1 = tmp1
        local_hum1 = hum1
        local_tmp2 = tmp2
        local_hum2 = hum2
    
    with display_lock:
        try:
            if DISPLAY_MODES[current_mode] == 'sensors':
                # Show both sensors
                line1 = "T1:{:.1f}  H1:{}% ".format(local_tmp1, local_hum1) if local_tmp1 is not None else "T1:Not Connected"
                line2 = "T2:{:.1f}  H2:{}% ".format(local_tmp2, local_hum2) if local_tmp2 is not None else "T2:Not Connected"
                display.lcd_display_string(line1, 1)
                display.lcd_display_string(line2, 2)
                
            elif DISPLAY_MODES[current_mode] == 'clock':
                # Show clock
                now = datetime.datetime.now()
                time_str = now.strftime('%H:%M:%S')
                date_str = now.strftime('%d/%m/%Y')
                display.lcd_display_string("{:^16}".format(time_str), 1)
                display.lcd_display_string("{:^16}".format(date_str), 2)
                
            elif DISPLAY_MODES[current_mode] == 'sensor1':
                # Show sensor 1 only
                if local_tmp1 is not None:
                    line1 = "T1:{:.1f}C".format(local_tmp1)
                    line2 = "H1:{}%".format(local_hum1)
                else:
                    line1 = "T1:Not Connected"
                    line2 = "---"
                display.lcd_display_string("{:^16}".format(line1), 1)
                display.lcd_display_string("{:^16}".format(line2), 2)
                
            elif DISPLAY_MODES[current_mode] == 'sensor2':
                # Show sensor 2 only
                if local_tmp2 is not None:
                    line1 = "T2:{:.1f}C".format(local_tmp2)
                    line2 = "H2:{}%".format(local_hum2)
                else:
                    line1 = "T2:Not Connected"
                    line2 = "---"
                display.lcd_display_string("{:^16}".format(line1), 1)
                display.lcd_display_string("{:^16}".format(line2), 2)
                
            elif DISPLAY_MODES[current_mode] == 'wifi':
                # Show WiFi status
                ip_address, wifi_status = get_wifi_status()
                display.lcd_display_string("{:^16}".format("WiFi Status"), 1)
                display.lcd_display_string("{:^16}".format(wifi_status), 2)
                
        except Exception as e:
            logging.error(datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S') + "  -- Display update error: " + str(e))

def read_sensors():
    """Background thread to read sensors continuously"""
    global tmp1, hum1, tmp2, hum2
    
    while running:
        try:
            result1 = sensor1.sample(samples=3)
            if result1.get('valid') == True:
                with sensor_lock:
                    tmp1 = result1.get('temp_c')
                    hum1 = result1.get('humidity')
                logging.info(datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S') + "  -- sensor 1 read")
        except Exception as e:
            logging.debug(datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S') + "  -- Sensor 1 error: " + str(e))
        
        try:
            result2 = sensor2.sample(samples=3)
            if result2.get('valid') == True:
                with sensor_lock:
                    tmp2 = result2.get('temp_c')
                    hum2 = result2.get('humidity')
                logging.info(datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S') + "  -- sensor 2 read")
        except Exception as e:
            logging.debug(datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S') + "  -- Sensor 2 error: " + str(e))
        
        # Read sensors every 5 seconds (sensors don't need to be read every 2 seconds)
        time.sleep(5)

def get_influx_client():
    """Get or create InfluxDB client"""
    global influx_client
    if influx_client is None:
        influxdbName = 'temperature'
        influx_client = influxdb.InfluxDBClient(influxHost, 8086, influxUser, influxPasswd, influxdbName)
    return influx_client

def save_to_influxdb():
    """Save sensor data to InfluxDB"""
    global old_time
    
    # Get current sensor values in a thread-safe manner
    with sensor_lock:
        local_tmp1 = tmp1
        local_hum1 = hum1
        local_tmp2 = tmp2
        local_hum2 = hum2
    
    current_time = datetime.datetime.now(datetime.timezone.utc)
    
    if (current_time - datetime.timedelta(minutes=5) > old_time and 
        all(v is not None for v in [local_tmp1, local_hum1, local_tmp2, local_hum2])):
        
        logging.info(datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S') + "  -- Start saving to db...")
        old_time = current_time
        
        influx_metric = [{
             'measurement': 'TemperatureSensor',
             'time': current_time.strftime('%Y-%m-%dT%H:%M:%SZ'),
             'fields': {
                 'temperature1': local_tmp1,
                 'humidity1': local_hum1,
                 'temperature2': local_tmp2,
                 'humidity2': local_hum2
             }
        }]
        
        try:
            db = get_influx_client()
            db.write_points(influx_metric)
            logging.info(current_time.strftime('%Y-%m-%dT%H:%M:%S') + "  -- Saved to db")
        except Exception as e:
            logging.error(current_time.strftime('%Y-%m-%dT%H:%M:%S') + "  -- ERROR Saving to db: " + str(e))
            # Reset client on error to force reconnection
            global influx_client
            influx_client = None

def signal_handler(signum, frame):
    """Handle shutdown signals gracefully"""
    global running
    logging.info(datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S') + "  -- Shutting down...")
    running = False
    
    # Cleanup
    if display is not None:
        try:
            display.lcd_display_string("Goodbye!", 1)
            display.lcd_display_string("{:^16}".format("Shutting down"), 2)
            time.sleep(1)
            display.lcd_clear()
        except:
            pass
    
    if pi is not None:
        try:
            pi.stop()
        except:
            pass
    
    if influx_client is not None:
        try:
            influx_client.close()
        except:
            pass
    
    logging.info(datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S') + "  -- Cleanup complete")
    sys.exit(0)

# Register signal handlers
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# Track last display update to avoid flickering
last_display_update = time.time()
display_update_interval = 1  # Update display every 1 second

# Initial display update
update_display()

# Start sensor reading thread
sensor_thread = threading.Thread(target=read_sensors, daemon=True)
sensor_thread.start()
logging.info(datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S') + "  -- Sensor thread started")

while running:
    # Update display based on current mode (for clock mode which needs frequent updates)
    current_time = time.time()
    if current_time - last_display_update >= display_update_interval:
        update_display()
        last_display_update = current_time

    # Saving data to InfluxDB
    try:
        save_to_influxdb()
    except Exception as e:
        logging.error(datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S') + "  -- Error in data saving loop: " + str(e))

    # Shorter sleep for more responsive display updates
    time.sleep(0.5)
