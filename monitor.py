#! /usr/bin/env python
import drivers
from pigpio_dht import DHT22

sensor1 = DHT22(17)
sensor2 = DHT22(27)
try:
    display = drivers.Lcd()
except:
    pass
while True:
    try:
        result1 = sensor1.sample(samples=3)
        if (result1.get('valid') == True):
            tmp1 = result1.get('temp_c')
            hum1 = result1.get('humidity')
            display.lcd_display_string("T1:{:.1f}  H1:{}% ".format(tmp1, hum1), 1)
    except:
        pass

    try:
        result2 = sensor2.sample(samples=3)
        if (result2.get('valid') == True):
            tmp2 = result2.get('temp_c')
            hum2 = result2.get('humidity')
            display.lcd_display_string("T2:{:.1f}  H2:{}% ".format(tmp2, hum2), 2)
    except:
        pass

