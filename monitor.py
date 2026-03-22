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
# Sensor 1 is on channel 6 (confirmed working)
SENSOR_CHANNELS = [0, 1, 2, 3]  # All sensors on channel 6 for now (same sensor)

# AHT20 Sensor I2C addresses (all same address, different multiplexer channels)
# Default AHT20 address is 0x38
SENSOR_I2C_ADDRESSES = [0x38, 0x38, 0x38, 0x38]  # 4 sensors, same address by default
# If you have different addresses, e.g., [0x38, 0x39, 0x3C, 0x3D]

# InfluxDB Connection Details
influxHost = 'localhost'
influxUser = 'admin'

# FIX #1: Wrap secret file read in try/except to avoid unhandled crash
try:
    with open(os.path.dirname(os.path.abspath(__file__)) + '/secretstring', 'r') as f:
        influxPasswd = f.readline().strip()
except Exception as e:
    logging.error(f"FATAL: Could not read secretstring file: {e}")
    influxPasswd = ''

old_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5)

# Thread-safe sensor data storage
sensor_lock = threading.Lock()
# Store sensor data: list of dicts with 'temp' and 'humidity'
# We have 2 sensors working (on channel 6), but keep array size 4 for compatibility
sensor_data = [
    {'temp': None, 'humidity': None},  # Sensor 1 (channel 6)
    {'temp': None, 'humidity': None},  # Sensor 2 (channel 6 - same physical sensor)
    {'temp': None, 'humidity': None},  # Sensor 3 (not connected)
    {'temp': None, 'humidity': None}   # Sensor 4 (not connected)
]

# Display modes - expanded for 4 sensors
# New layout: humidity pairs, temp pairs, temp-humidity pairs, clock
DISPLAY_MODES = ['humidity_pairs', 'temp_pairs', 'temp_humidity_pairs1', 'temp_humidity_pairs2', 'clock']
current_mode = 0  # Start with humidity display

# Lock for thread-safe display updates
display_lock = threading.Lock()

# Flag for graceful shutdown
running = True

# InfluxDB client (reusable connection)
influx_client = None
# FIX #10: Lock for thread-safe InfluxDB client access
influx_client_lock = threading.Lock()

# AHT20 sensor objects
aht_sensors = [None, None, None, None]

# FIX #7: Initialize pi to None before try block so signal_handler never gets NameError
pi = None

# SMBus for TCA9548A communication - use a fresh bus each time
def select_tca_channel(channel, retries=5):
    """Select a channel on the TCA9548A I2C multiplexer with retries"""
    for attempt in range(retries):
        # FIX #3: Use context manager so bus is always closed even on exception
        try:
            with smbus2.SMBus(1) as bus:
                # TCA9548A control register: set the channel (bit 0-2 for channel)
                # Channel 0 = 0x01, Channel 1 = 0x02, Channel 2 = 0x04, etc.
                control = 1 << channel
                bus.write_byte(TCA9548A_ADDRESS, control)
                time.sleep(0.05)  # Even longer delay for channel switch
            return True
        except IOError as e:
            if attempt < retries - 1:  # Don't log on last attempt to avoid spam
                logging.debug(f"TCA9548A channel {channel} attempt {attempt+1} failed: {e}")
            time.sleep(0.1)  # Wait before retry
        except Exception as e:
            logging.error(f"Failed to select TCA9548A channel {channel}: {e}")
            return False
    return False  # Silently fail after retries to avoid log spam

try:
    display = drivers.Lcd()
    logging.warning(datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S') + "  -- LCD started")
except Exception as e:
    logging.error(datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S') + f"  -- LCD error: {e}")
    display = None

def initialize_aht_sensors():
    """Initialize AHT20 sensors on different TCA9548A channels"""
    global aht_sensors
    
    # First, verify TCA9548A is reachable with retry
    tca_found = False
    for attempt in range(5):
        # FIX #4: Use context manager so bus is always closed even on exception
        try:
            with smbus2.SMBus(1) as bus:
                # Simply try to communicate with the TCA9548A
                bus.write_byte(TCA9548A_ADDRESS, 0x00)  # Try to write to it
                time.sleep(0.05)
                # Try to read back
                device_id = bus.read_byte(TCA9548A_ADDRESS)
            tca_found = True
            logging.warning(f"TCA9548A multiplexer found at address 0x{TCA9548A_ADDRESS:02X}")
            break
        except Exception as e:
            logging.warning(f"TCA9548A attempt {attempt+1} failed: {e}")
            time.sleep(0.2)
    
    if not tca_found:
        logging.error(f"TCA9548A multiplexer NOT found at address 0x{TCA9548A_ADDRESS:02X}")
        return
    
    # Try to initialize each sensor on its assigned channel
    for i, addr in enumerate(SENSOR_I2C_ADDRESSES):
        channel = SENSOR_CHANNELS[i] if i < len(SENSOR_CHANNELS) else i
        
        # FIX #5: Track i2c handle so it can be closed on failure
        i2c = None
        try:
            # Select the channel on TCA9548A
            if not select_tca_channel(channel, retries=3):
                logging.warning(f"AHT20 sensor {i+1} failed to select channel {channel}")
                aht_sensors[i] = None
                continue
            
            # Increased delay after channel selection to let the multiplexer settle
            time.sleep(0.2)
            
            # Create AHT20 sensor object
            i2c = board.I2C()
            aht_sensors[i] = adafruit_ahtx0.AHTx0(i2c, address=addr)
            
            # Delay before reading from sensor to let it stabilize
            time.sleep(0.2)
            
            # Quick test read to verify sensor is present
            _ = aht_sensors[i].temperature
            logging.warning(f"AHT20 sensor {i+1} initialized on channel {channel} at address 0x{addr:02X}")
        except Exception as e:
            logging.warning(f"AHT20 sensor {i+1} NOT found on channel {channel} at address 0x{addr:02X}: {e}")
            # Close i2c handle if sensor init failed
            if i2c is not None:
                try:
                    i2c.deinit()
                except Exception:
                    pass
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

    # FIX #6: Use an event to signal display update from ISR instead of calling update_display() directly
    display_update_event = threading.Event()

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
            # Signal main loop to update display instead of doing it here
            display_update_event.set()
    
    # Set up callback on BOTH edges (rising and falling)
    pi.callback(BUTTON_PIN, pigpio.EITHER_EDGE, button_callback)
    
except Exception as e:
    pi = None
    logging.error(datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S') + "  -- Button initialization failed: " + str(e))
    display_update_event = threading.Event()

def get_wifi_status():
    """Get WiFi status and IP address"""
    try:
        # Get IP address
        result = subprocess.run(['hostname', '-I'], capture_output=True, text=True, timeout=5)
        ip_address = result.stdout.strip() if result.stdout else 'No IP'
        
        # FIX #9: Add timeout to ping subprocess.run call
        result = subprocess.run(['ping', '-c', '1', '-W', '2', '8.8.8.8'], capture_output=True, text=True, timeout=5)
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
            if DISPLAY_MODES[current_mode] == 'humidity_pairs':
                # Show humidity pairs: H1: H2 on line 1, H3: H4 on line 2
                h1 = local_sensor_data[0]['humidity']
                h2 = local_sensor_data[1]['humidity']
                h3 = local_sensor_data[2]['humidity']
                h4 = local_sensor_data[3]['humidity']

                h1_str = "{:3d}".format(int(h1)) if h1 is not None else " --"
                h2_str = "{:3d}".format(int(h2)) if h2 is not None else " --"
                h3_str = "{:3d}".format(int(h3)) if h3 is not None else " --"
                h4_str = "{:3d}".format(int(h4)) if h4 is not None else " --"

                # FIX #12: Clamp display strings to 16 chars
                line1 = "H1:{}%  H2:{}%".format(h1_str, h2_str)[:16]
                line2 = "H3:{}%  H4:{}%".format(h3_str, h4_str)[:16]
                display.lcd_display_string(line1, 1)
                display.lcd_display_string(line2, 2)
                
            elif DISPLAY_MODES[current_mode] == 'temp_pairs':
                # Show temperature pairs: T1: T2 on line 1, T3: T4 on line 2
                t1 = local_sensor_data[0]['temp']
                t2 = local_sensor_data[1]['temp']
                t3 = local_sensor_data[2]['temp']
                t4 = local_sensor_data[3]['temp']

                t1_str = "{:.1f}".format(t1) if t1 is not None else "  --"
                t2_str = "{:.1f}".format(t2) if t2 is not None else "  --"
                t3_str = "{:.1f}".format(t3) if t3 is not None else "  --"
                t4_str = "{:.1f}".format(t4) if t4 is not None else "  --"

                # FIX #12: Clamp display strings to 16 chars
                line1 = "T1:{}  T2:{}".format(t1_str, t2_str)[:16]
                line2 = "T3:{}  T4:{}".format(t3_str, t4_str)[:16]
                display.lcd_display_string(line1, 1)
                display.lcd_display_string(line2, 2)
                
            elif DISPLAY_MODES[current_mode] == 'temp_humidity_pairs1':
                # Show T1:H1 and T2:H2
                t1 = local_sensor_data[0]['temp']
                h1 = local_sensor_data[0]['humidity']
                t2 = local_sensor_data[1]['temp']
                h2 = local_sensor_data[1]['humidity']

                t1_str = "{:.1f}".format(t1) if t1 is not None else "  --"
                h1_str = "{:3d}".format(int(h1)) if h1 is not None else " --"
                t2_str = "{:.1f}".format(t2) if t2 is not None else "  --"
                h2_str = "{:3d}".format(int(h2)) if h2 is not None else " --"

                # FIX #12: Clamp display strings to 16 chars
                line1 = "T1:{}C H1:{}%".format(t1_str, h1_str)[:16]
                line2 = "T2:{}C H2:{}%".format(t2_str, h2_str)[:16]
                display.lcd_display_string(line1, 1)
                display.lcd_display_string(line2, 2)
                
            elif DISPLAY_MODES[current_mode] == 'temp_humidity_pairs2':
                # Show T3:H3 and T4:H4
                t3 = local_sensor_data[2]['temp']
                h3 = local_sensor_data[2]['humidity']
                t4 = local_sensor_data[3]['temp']
                h4 = local_sensor_data[3]['humidity']

                t3_str = "{:.1f}".format(t3) if t3 is not None else "  --"
                h3_str = "{:3d}".format(int(h3)) if h3 is not None else " --"
                t4_str = "{:.1f}".format(t4) if t4 is not None else "  --"
                h4_str = "{:3d}".format(int(h4)) if h4 is not None else " --"

                # FIX #12: Clamp display strings to 16 chars
                line1 = "T3:{}C H3:{}%".format(t3_str, h3_str)[:16]
                line2 = "T4:{}C H4:{}%".format(t4_str, h4_str)[:16]
                display.lcd_display_string(line1, 1)
                display.lcd_display_string(line2, 2)
                
            elif DISPLAY_MODES[current_mode] == 'clock':
                # Show clock
                now = datetime.datetime.now()
                time_str = now.strftime('%H:%M:%S')
                date_str = now.strftime('%d/%m/%Y')
                display.lcd_display_string("{:^16}".format(time_str), 1)
                display.lcd_display_string("{:^16}".format(date_str), 2)
                
        except Exception as e:
            logging.error(datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S') + "  -- Display update error: " + str(e))

def read_sensors():
    """Background thread to read AHT20 sensors continuously"""
    global sensor_data
    
    while running:
        # FIX #11: Wrap entire loop body to prevent silent thread death
        try:
            for i, sensor in enumerate(aht_sensors):
                if sensor is not None:
                    try:
                        # Select the correct TCA9548A channel before reading
                        channel = SENSOR_CHANNELS[i] if i < len(SENSOR_CHANNELS) else i
                        if not select_tca_channel(channel):
                            logging.debug(f"AHT20 sensor {i+1}: failed to select channel {channel}")
                            with sensor_lock:
                                sensor_data[i]['temp'] = None
                                sensor_data[i]['humidity'] = None
                            continue
                        
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
        except Exception as e:
            logging.error(f"Unexpected error in sensor read loop: {e}")
            time.sleep(1)  # Brief pause before retrying
        
        # Read sensors every 5 seconds
        time.sleep(5)

def get_influx_client():
    """Get or create InfluxDB client - thread safe"""
    global influx_client
    # FIX #10: Use lock for thread-safe client creation
    with influx_client_lock:
        if influx_client is None:
            influxdbName = 'temperature'
            influx_client = influxdb.InfluxDBClient(influxHost, 8086, influxUser, influxPasswd, influxdbName)
        return influx_client

def save_to_influxdb():
    """Save sensor data to InfluxDB"""
    # FIX #2: Declare both globals at the top of the function
    global old_time, influx_client
    
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
                # FIX #10: Reset client under lock to force reconnection
                with influx_client_lock:
                    influx_client = None

def signal_handler(signum, frame):
    """Handle shutdown signals gracefully"""
    global running
    logging.info(datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S') + "  -- Shutting down...")
    running = False
    
    # Cleanup
    if display is not None:
        # FIX #8: Use except Exception instead of bare except
        try:
            display.lcd_display_string("Goodbye!", 1)
            display.lcd_display_string("{:^16}".format("Shutting down"), 2)
            time.sleep(1)
            display.lcd_clear()
        except Exception:
            pass
    
    # FIX #7: pi is always defined (initialized to None before try block)
    if pi is not None:
        try:
            pi.stop()
        except Exception:
            pass
    
    if influx_client is not None:
        try:
            influx_client.close()
        except Exception:
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
    # FIX #6: Check display_update_event set by button callback (avoids I/O in ISR)
    if display_update_event.is_set():
        display_update_event.clear()
        update_display()
        last_display_update = time.time()

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
