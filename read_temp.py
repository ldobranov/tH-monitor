#!/usr/bin/python3
import drivers
from pigpio_dht import DHT22
import datetime
import os
import threading
from influxdb import client as influxdb
sensor1 = DHT22(17)
sensor2 = DHT22(27)
#InfluxDB Connection Details
influxHost = 'localhost'
influxUser = 'admin'
with open(os.path.dirname(os.path.abspath(__file__)) + '/secretstring', 'r') as f:
    influxPasswd = f.readline().strip()
f.close()
try:
    result1 = sensor1.sample(samples=3)
    if (result1.get('valid') == True):
        temperature1 = result1.get('temp_c')
        humidity1 = result1.get('humidity')
except:
    pass
try:
    result2 = sensor2.sample(samples=3)
    if (result2.get('valid') == True):
        temperature2 = result1.get('temp_c')
        humidity2 = result1.get('humidity')
except:
    pass
influxdbName = 'temperature'
current_time = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
influx_metric = [{
     'measurement': 'TemperatureSensor',
     'time': current_time,
     'fields': {
         'temperature1': temperature1,
         'humidity1': humidity1,
         'temperature2': temperature2,
         'humidity2': humidity2
     }
}]
#Saving data to InfluxDB
try:
    db = influxdb.InfluxDBClient(influxHost, 8086, influxUser, influxPasswd, influxdbName)
    db.write_points(influx_metric)
finally:
    db.close()
