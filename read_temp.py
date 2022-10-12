#!/usr/bin/python3

import Adafruit_DHT
import datetime
import os
import threading
from influxdb import client as influxdb

#InfluxDB Connection Details
influxHost = 'localhost'
influxUser = 'admin'
with open(os.path.dirname(os.path.abspath(__file__)) + '/secretstring', 'r') as f:
    influxPasswd = f.readline().strip()
f.close()

humidity, temperature = Adafruit_DHT.read_retry(22, 17)
humidity1, temperature1 = Adafruit_DHT.read_retry(22, 27)

influxdbName = 'temperature'
current_time = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
influx_metric = [{
     'measurement': 'TemperatureSensor',
     'time': current_time,
     'fields': {
         'temperature1': temperature,
         'humidity1': humidity,
         'temperature2': temperature1,
         'humidity2': humidity1
     }
}]
#Saving data to InfluxDB
try:
    db = influxdb.InfluxDBClient(influxHost, 8086, influxUser, influxPasswd, influxdbName)
    db.write_points(influx_metric)
finally:
    db.close()
