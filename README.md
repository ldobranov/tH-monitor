# Temperature and humidity monitor
This repository contains all the code for interfacing with a **16x2 I2C LCD**, **DHT11, DHT22** sensors, **InfluDB** and **Grafna**

# Installation
- Install git
  ```
  sudo apt update && sudo apt install git -y
  ```

- Clone the repo in your home directory
  ```
  git clone https://github.com/ldobranov/tH-monitor.git
  cd tH-monitor/
  ```

- Run the automatic installation script with `sudo` permission
  ```
  sudo ./install.sh
  ```

- Finally we will need to automate the script by running it as a cronjob run the following command to open the cronjob editor:
  ```
  crontab -e
  ```

- Add the following line to the file to run the script exactly every 5 minutes:
  ```
  # m h  dom mon dow   command
  0,5,10,15,20,25,30,35,40,45,50,55   *    *    *    *  python /home/pi/lcd/read_temp.py
  ```
- To enable authentication, enter the influx cli with the command influx. Once we are in the influx cli, we need to enter the following query to create the admin account. In this step, you best replace pass in the query with one of your own choice. Keep in mind that we will be using these credentials for the rest of the project.
  ```
  CREATE USER "admin" WITH PASSWORD 'pass' WITH ALL PRIVILEGES
  SHOW users
  ```
- In the following step we will create the database. To do this we need to enter the influx cli again, but this time we need to provide the credentials of the admin account:
  ```
  influx -username admin -password pass
  ```
- We create the temperature database with following query and exit the cli:
  ```
  CREATE DATABASE "temperature"
  quit
  ```
- During the installation, pay attention to any messages about `python` and `python3` usage, as they inform which version you should use to interface with the LCD driver.  For example:
  ```
  [LCD] [INFO] You may use either 'python' or 'python3' to interface with the lcd.
  ```
  or alternatively,
  ```
  [LCD] [INFO] Use 'python3' to interface with the lcd.
  ```

- At the end of the installation script, you'll be prompted to reboot the RPi to apply the changes made to `/boot/config.txt` and `/etc/modules`.

- After rebooting, go to grafan page:
  ```
  http://localhost:3000/
  ```
  or
  ```
  http://<Raspberry Pi IP address>:3000/
  ```

[top :arrow_up:](#)
