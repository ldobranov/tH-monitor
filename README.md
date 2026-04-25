# Temperature and Humidity Monitor

This repository contains all the code for interfacing with a **16x2 I2C LCD**, **DHT11, DHT22** sensors, **InfluxDB** and **Grafana**.

## Installation

### Prerequisites
- A Raspberry Pi (tested on Raspberry Pi 3B+ and 4)
- Raspberry Pi OS (formerly Raspbian) installed
- Internet connection

### Step-by-Step Guide

1. **Install git** (if not already installed)
   ```bash
   sudo apt update && sudo apt install git -y
   ```

2. **Clone the repository** in your home directory
   ```bash
   git clone https://github.com/ldobranov/tH-monitor.git
   cd tH-monitor/
   ```

3. **Run the automatic installation script** with `sudo` permission
   ```bash
   sudo ./install.sh
   ```
   The installation script will:
   - Install required packages (i2c-tools, python-smbus, python3-smbus, pigpio, hostapd, dnsmasq, influxdb, grafana, etc.)
   - Set up the LCD service
   - Set up the temperature/humidity monitoring service
   - Set up the WiFi manager service (for creating an access point)
   - Enable InfluxDB and Grafana services
   - Configure I2C on boot
   - Prompt to reboot the Raspberry Pi

4. **During installation**, pay attention to messages about Python version usage for the LCD driver:
   - If both `python-smbus` and `python3-smbus` are installed, you can use either `python` or `python3`
   - If only `python3-smbus` is installed, use `python3`
   - If only `python-smbus` is installed, use `python`

5. **After rebooting**, the system will automatically start the following services:
   - `lcd.service` - Displays temperature and humidity on the LCD
   - `monitor.service` - Reads sensors and writes data to InfluxDB
   - `wifi-manager.service` - Manages WiFi and creates an access point if needed
   - `influxdb` - Time series database
   - `grafana-server` - Visualization dashboard

6. **To enable authentication in InfluxDB** (recommended for security):
   ```bash
   influx
   ```
   Then in the InfluxDB CLI:
   ```
   CREATE USER "admin" WITH PASSWORD 'pass' WITH ALL PRIVILEGES
   SHOW users
   quit
   ```
   Replace `'pass'` with a strong password of your choice.

7. **Create the database** for temperature data:
   ```bash
   influx -username admin -password your_secure_password
   ```
   Then in the InfluxDB CLI:
   ```
   CREATE DATABASE "temperature"
   quit
   ```

8. **Access Grafana** after reboot:
   - Open a web browser and go to: http://localhost:3000/
   - Or from another device: http://<Raspberry_Pi_IP_address>:3000/
   - Default login: admin/admin (change this immediately after first login!)

9. **Configure Grafana** to use InfluxDB:
   - Add InfluxDB as a data source
   - Configure the database, user, and password you set up earlier
   - Import or create dashboards to visualize temperature and humidity data

## Services Overview

- **lcd.service**: Controls the 16x2 I2C LCD display
- **monitor.service**: Reads DHT11/DHT22 sensors and writes to InfluxDB
- **wifi-manager.service**: Creates a WiFi access point for initial configuration
- **influxdb**: Time series database for sensor data
- **grafana-server**: Web interface for data visualization

## Configuration Files

- LCD configuration: `configs/` directory
- Grafana provisioning: `grafana.json` (for dashboard provisioning)
- WiFi manager HTML template: `templates/wifi_manager.html`
- WiFi manager CSS: `static/style.css`

## Troubleshooting

- If the LCD doesn't display text, check the I2C connection and run `sudo i2cdetect -y 1` to find the LCD address
- If services fail to start, check their status with `sudo systemctl status <service_name>`
- View logs with `sudo journalctl -u <service_name> -f`
- Ensure I2C is enabled in `/boot/config.txt` (should contain `dtparam=i2c_arm=on`)

## Notes

- This project is designed for Raspberry Pi but may work on other Linux systems with I2C support
- The installation script modifies system files (`/boot/config.txt`, `/etc/modules`, etc.) - backup important data before proceeding
- For production use, consider changing default passwords and securing your network

[top :arrow_up:](#)
