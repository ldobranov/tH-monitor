#! /usr/bin/env python3
import logging
import drivers
import datetime
import os
import time
import subprocess
import signal
import sys
import board # type: ignore
import adafruit_ahtx0 # type: ignore
from influxdb import client as influxdb # type: ignore
import threading
import smbus2 # type: ignore

#logmode = logging.DEBUG
logmode = logging.WARNING

logging.basicConfig(filename="/home/raspberry/tH-monitor/log_monitor.txt", level=logmode)

# Configuration
BUTTON_PIN = 18  # GPIO pin for button

# TCA9548A I2C Multiplexer Address
# Default address is 0x70 (decimal 112)
TCA9548A_ADDRESS = 0x70

# TCA9548A Channel mapping for each sensor (channels 0-7)
# Update these channels based on how you wired your sensors
# Example: If sensor 1 is on channel 0, sensor 2 on channel 1, etc.
SENSOR_CHANNELS = [0, 1, 2, 3]  # Channels for sensors 1-4

# AHT20 Sensor I2C addresses (all same address, different multiplexer channels)
# Default AHT20 address is 0x38
SENSOR_I2C_ADDRESSES = [0x38, 0x38, 0x38, 0x38]  # 4 sensors, same address by default
# If you have different addresses, e.g., [0x38, 0x39, 0x3C, 0x3D]

#InfluxDB Connection Details
influxHost = 'localhost'
influxUser = 'admin'
with open(os.path.dirname(os.path.abspath(__file__)) + '/secretstring', 'r') as f:
    influxPasswd = f.readline().strip()

old_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5)

# Thread-safe sensor data storage
sensor_lock = threading.Lock()
# Store sensor data: list of dicts with 'temp' and 'humidity'
sensor_data = [
    {'temp': None, 'humidity': None},
    {'temp': None, 'humidity': None},
    {'temp': None, 'humidity': None},
    {'temp': None, 'humidity': None}
]

# Display modes - expanded for 4 sensors
DISPLAY_MODES = ['sensors12', 'sensors34', 'clock', 'sensor1', 'sensor2', 'sensor3', 'sensor4']
current_mode = 0  # Start with sensor display

# Lock for thread-safe display updates
display_lock = threading.Lock()

# Flag for graceful shutdown
running = True

# InfluxDB client (reusable connection)
influx_client = None

# AHT20 sensor objects
aht_sensors = [None, None, None, None]

# SMBus for TCA9548A communication
tca_bus = None

def select_tca_channel(channel):
    """Select a channel on the TCA9548A I2C multiplexer"""
    global tca_bus
    if tca_bus is None:
        try:
            tca_bus = smbus2.SMBus(1)
        except Exception as e:
            logging.error(f"Failed to open SMBus for TCA9548A: {e}")
            return False
    
    try:
        # TCA9548A control register: set the channel (bit 0-2 for channel, bit 4 for enable)
        # Channel 0 = 0x01, Channel 1 = 0x02, Channel 2 = 0x04, etc.
        control = 1 << channel
        tca_bus.write_byte(TCA9548A_ADDRESS, control)
        time.sleep(0.005)  # Small delay for channel switch
        return True
    except Exception as e:
        logging.error(f"Failed to select TCA9548A channel {channel}: {e}")
        return False

try:
    display = drivers.Lcd()
    logging.warning(datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S') + "  -- LCD started")
except:
    logging.error(datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S') + "  -- LCD error")
    display = None
    pass

def initialize_aht_sensors():
    """Initialize AHT20 sensors on different TCA9548A channels"""
    global aht_sensors
    
    # First, verify TCA9548A is reachable
    try:
        bus = smbus2.SMBus(1)
        device_id = bus.read_byte(TCA9548A_ADDRESS)
        bus.close()
        logging.warning(f"TCA9548A multiplexer found at address 0x{TCA9548A_ADDRESS:02X}")
    except Exception as e:
        logging.error(f"TCA9548A multiplexer NOT found at address 0x{TCA9548A_ADDRESS:02X}: {e}")
        return
    
    # Try to initialize each sensor on its assigned channel
    for i, addr in enumerate(SENSOR_I2C_ADDRESSES):
        channel = SENSOR_CHANNELS[i] if i < len(SENSOR_CHANNELS) else i
        
        try:
            # Select the channel on TCA9548A
            if not select_tca_channel(channel):
                logging.warning(f"AHT20 sensor {i+1} failed to select channel {channel}")
                aht_sensors[i] = None
                continue
            
            # Small delay after channel selection
            time.sleep(0.05)
            
            # Use smbus2 to check if device exists at address on selected channel
            bus = smbus2.SMBus(1)
            # First select channel again (bus may have been closed)
            bus.write_byte(TCA9548A_ADDRESS, 1 << channel)
            time.sleep(0.005)
            device_id = bus.read_byte(addr)
            bus.close()
            
            # Check if we got a valid response (AHT20 should respond)
            if device_id == 0:
                logging.warning(f"AHT20 sensor {i+1} NOT found at channel {channel}, address 0x{addr:02X}: no response")
                aht_sensors[i] = None
                continue
            
            # Create AHT20 sensor object
            i2c = board.I2C()
            aht_sensors[i] = adafruit_ahtx0.AHTx0(i2c, address=addr)
            logging.warning(f"AHT20 sensor {i+1} initialized on channel {channel} at address 0x{addr:02X}")
        except Exception as e:
            logging.warning(f"AHT20 sensor {i+1} NOT found on channel {channel} at address 0x{addr:02X}: {e}")
            aht_sensors[i] = None

# Initialize sensors
initialize_aht_sensors()

# Initialize pigpio for button with callback
try:
    import pigpio # type: ignore
    pi = pigpio.pi()
    # Set up button pin with pull-down resistor (button connects to +3.3V)
    pi.set_mode(BUTTON_PIN, pigpio.INPUT)
    pi.set_pull_up_down(BUTTON_PIN, pigpio.PUD_DOWN)
    logging.warning(datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S') + "  -- Button initialized on GPIO" + str(BUTTON_PIN))
    
    # Track last button press time for debouncing
    last_button_press_time = 0
    button_debounce_ms = 500  # 500ms debounce time - increased to filter noise
    
    # Minimum press duration to ignore noise (microseconds)
    # Only accept button presses that last at least 20ms
    MIN_PRESS_DURATION_MS = 20
    
    # Button press tracking for long press detection
    button_pressed_time = None
    LONG_PRESS_DURATION_MS = 10000  # 10 seconds long press
    
    # Button callback function - triggered on EITHER edge (press or release)
    def button_callback(gpio, level, tick):
        global current_mode, last_button_press_time, button_pressed_time
        
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
            
            # Ignore short noise < 20ms
            if press_duration < MIN_PRESS_DURATION_MS:
                return
            
            # Short press - cycle display modes
            last_button_press_time = current_time_ms
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

def update_display():
    """Update LCD based on current display mode - thread safe"""
    if display is None:
        return
    
    # Get current sensor values in a thread-safe manner
    with sensor_lock:
        local_sensor_data = [s.copy() for s in sensor_data]
    
    with display_lock:
        try:
            if DISPLAY_MODES[current_mode] == 'sensors12':
                # Show sensors 1 and 2
                s1 = local_sensor_data[0]
                s2 = local_sensor_data[1]
                if s1['temp'] is not None:
                    line1 = "T1:{:.1f}C H1:{}%".format(s1['temp'], int(s1['humidity']) if s1['humidity'] else '--')
                else:
                    line1 = "T1: Not Connected"
                if s2['temp'] is not None:
                    line2 = "T2:{:.1f}C H2:{}%".format(s2['temp'], int(s2['humidity']) if s2['humidity'] else '--')
                else:
                    line2 = "T2: Not Connected"
                display.lcd_display_string(line1, 1)
                display.lcd_display_string(line2, 2)
                
            elif DISPLAY_MODES[current_mode] == 'sensors34':
                # Show sensors 3 and 4
                s3 = local_sensor_data[2]
                s4 = local_sensor_data[3]
                if s3['temp'] is not None:
                    line1 = "T3:{:.1f}C H3:{}%".format(s3['temp'], int(s3['humidity']) if s3['humidity'] else '--')
                else:
                    line1 = "T3: Not Connected"
                if s4['temp'] is not None:
                    line2 = "T4:{:.1f}C H4:{}%".format(s4['temp'], int(s4['humidity']) if s4['humidity'] else '--')
                else:
                    line2 = "T4: Not Connected"
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
                s1 = local_sensor_data[0]
                if s1['temp'] is not None:
                    line1 = "T1:{:.1f}C".format(s1['temp'])
                    line2 = "H1:{}%".format(int(s1['humidity']) if s1['humidity'] else '--')
                else:
                    line1 = "T1:Not Connected"
                    line2 = "---"
                display.lcd_display_string("{:^16}".format(line1), 1)
                display.lcd_display_string("{:^16}".format(line2), 2)
                
            elif DISPLAY_MODES[current_mode] == 'sensor2':
                # Show sensor 2 only
                s2 = local_sensor_data[1]
                if s2['temp'] is not None:
                    line1 = "T2:{:.1f}C".format(s2['temp'])
                    line2 = "H2:{}%".format(int(s2['humidity']) if s2['humidity'] else '--')
                else:
                    line1 = "T2:Not Connected"
                    line2 = "---"
                display.lcd_display_string("{:^16}".format(line1), 1)
                display.lcd_display_string("{:^16}".format(line2), 2)
                
            elif DISPLAY_MODES[current_mode] == 'sensor3':
                # Show sensor 3 only
                s3 = local_sensor_data[2]
                if s3['temp'] is not None:
                    line1 = "T3:{:.1f}C".format(s3['temp'])
                    line2 = "H3:{}%".format(int(s3['humidity']) if s3['humidity'] else '--')
                else:
                    line1 = "T3:Not Connected"
                    line2 = "---"
                display.lcd_display_string("{:^16}".format(line1), 1)
                display.lcd_display_string("{:^16}".format(line2), 2)
                
            elif DISPLAY_MODES[current_mode] == 'sensor4':
                # Show sensor 4 only
                s4 = local_sensor_data[3]
                if s4['temp'] is not None:
                    line1 = "T4:{:.1f}C".format(s4['temp'])
                    line2 = "H4:{}%".format(int(s4['humidity']) if s4['humidity'] else '--')
                else:
                    line1 = "T4:Not Connected"
                    line2 = "---"
                display.lcd_display_string("{:^16}".format(line1), 1)
                display.lcd_display_string("{:^16}".format(line2), 2)
                
        except Exception as e:
            logging.error(datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S') + "  -- Display update error: " + str(e))

def read_sensors():
    """Background thread to read AHT20 sensors continuously"""
    global sensor_data
    
    while running:
        for i, sensor in enumerate(aht_sensors):
            if sensor is not None:
                try:
                    # Select the correct TCA9548A channel before reading
                    channel = SENSOR_CHANNELS[i] if i < len(SENSOR_CHANNELS) else i
                    select_tca_channel(channel)
                    time.sleep(0.05)  # Allow channel to stabilize
                    
                    # Read temperature and humidity from AHT20
                    temperature = sensor.temperature
                    humidity = sensor.relative_humidity
                    
                    with sensor_lock:
                        sensor_data[i]['temp'] = temperature
                        sensor_data[i]['humidity'] = humidity
                    
                    logging.info(f"AHT20 sensor {i+1} (ch {channel}) read: {temperature:.1f}C, {humidity:.1f}%")
                except Exception as e:
                    logging.debug(f"AHT20 sensor {i+1} error: {e}")
                    with sensor_lock:
                        sensor_data[i]['temp'] = None
                        sensor_data[i]['humidity'] = None
            else:
                with sensor_lock:
                    sensor_data[i]['temp'] = None
                    sensor_data[i]['humidity'] = None
        
        # Read sensors every 5 seconds
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
        local_sensor_data = [s.copy() for s in sensor_data]
    
    current_time = datetime.datetime.now(datetime.timezone.utc)
    
    # Check if we have at least one valid sensor reading
    has_valid_data = any(s['temp'] is not None for s in local_sensor_data)
    
    if current_time - datetime.timedelta(minutes=5) > old_time and has_valid_data:
        
        logging.info(datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S') + "  -- Start saving to db...")
        old_time = current_time
        
        # Prepare fields for all 4 sensors
        fields = {}
        for i, s in enumerate(local_sensor_data):
            if s['temp'] is not None:
                fields[f'temperature{i+1}'] = s['temp']
            if s['humidity'] is not None:
                fields[f'humidity{i+1}'] = s['humidity']
        
        if fields:  # Only save if we have at least one field
            influx_metric = [{
                 'measurement': 'TemperatureSensor',
                 'time': current_time.strftime('%Y-%m-%dT%H:%M:%SZ'),
                 'fields': fields
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
