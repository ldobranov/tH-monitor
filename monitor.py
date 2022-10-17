#! /usr/bin/env python
import logging
import drivers
import datetime
import os
import threading
from pigpio_dht import DHT22
from influxdb import client as influxdb
#logmode = logging.DEBUG
logmode = logging.WARNING

logging.basicConfig(filename="/home/pi/tH-monitor/log_monitor.txt", level=logmode)

sensor1 = DHT22(17)
sensor2 = DHT22(27)

#InfluxDB Connection Details
influxHost = 'localhost'
influxUser = 'admin'
with open(os.path.dirname(os.path.abspath(__file__)) + '/secretstring', 'r') as f:
    influxPasswd = f.readline().strip()
f.close()

old_time = datetime.datetime.utcnow() - datetime.timedelta(minutes=5)

try:
    display = drivers.Lcd()
    logging.warning(datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S') + "  -- LCD started")
except:
    logging.error(datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S') + "  -- LCD error")
    pass

while True:
    try:
        result1 = sensor1.sample(samples=3)
        if (result1.get('valid') == True):
            tmp1 = result1.get('temp_c')
            hum1 = result1.get('humidity')
            display.lcd_display_string("T1:{:.1f}  H1:{}% ".format(tmp1, hum1), 1)
            logging.info(datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S') + "  -- sensor 1 readed")
    except:
        logging.debug(datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S') + "  -- Sensor 1 error")
        pass

    try:
        result2 = sensor2.sample(samples=3)
        if (result2.get('valid') == True):
            tmp2 = result2.get('temp_c')
            hum2 = result2.get('humidity')
            display.lcd_display_string("T2:{:.1f}  H2:{}% ".format(tmp2, hum2), 2)
            logging.info(datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S') + "  -- Sensor 2 readed")
    except:
        logging.debug(datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S') + "  -- Sensor 2 error")
        pass

    #Saving data to InfluxDB
    try:
        current_time = datetime.datetime.utcnow()
        if ( current_time - datetime.timedelta(minutes=5) > old_time):
            logging.info(datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S') + "  -- Start saving to db...")
            influxdbName = 'temperature'
            old_time = current_time
            influx_metric = [{
                 'measurement': 'TemperatureSensor',
                 'time': current_time.strftime('%Y-%m-%dT%H:%M:%SZ'),
                 'fields': {
                     'temperature1': tmp1,
                     'humidity1': hum1,
                     'temperature2': tmp2,
                     'humidity2': hum2
                 }
            }]
            try:
                db = influxdb.InfluxDBClient(influxHost, 8086, influxUser, influxPasswd, influxdbName)
                db.write_points(influx_metric)
                logging.info(current_time.strftime('%Y-%m-%dT%H:%M:%S') + "  -- Saved to db")

            except:
                logging.error(current_time.strftime('%Y-%m-%dT%H:%M:%S') + "  -- ERROR Saving to db")
                pass

            #finally:
            #db.close()
    except:
        logging.error(datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S') + "  --  NO LCD AND SENSORS CONECTED!!!")
