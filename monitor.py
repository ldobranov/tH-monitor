#! /usr/bin/env python
import time
import board
import adafruit_dht
import drivers

display = drivers.Lcd()
dht1 = adafruit_dht.DHT22(board.D17)
dht2 = adafruit_dht.DHT22(board.D27)

try:
  while True:
    try:
        tmp1 = dht1.temperature
        hum1 = dht1.humidity
        if (tmp1 == None or hum1 == None):
            tmp1=float(1.1)
            hum1=float(1.2)
    except RuntimeError as error:
        #print(error.args[0])
        continue
    try:
        tmp2 = dht2.temperature
        hum2 = dht2.humidity
        if (tmp2 == None or hum2 == None):
            tmp2=float(2.1)
            hum2=float(2.2)
    except RuntimeError as error:
        #print(error.args[0])
        continue
    time.sleep(5)
    print("T1:{:.1f}  H1:{}%  T2:{:.1f}  H2:{}%".format(tmp1, hum1, tmp2, hum2))
    display.lcd_display_string("T1:{:.1f}  H1:{}% ".format(tmp1, hum1), 1)
    display.lcd_display_string("T2:{:.1f}  H2:{}% ".format(tmp2, hum2), 2)

except KeyboardInterrupt:
    display.lcd_clear()
