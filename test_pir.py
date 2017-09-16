#!/usr/bin/env python
# -*- coding: utf-8 -*-

import RPi.GPIO as GPIO
import json
import signal
import sys
import time

def signalHandler(signal, frame):
    GPIO.cleanup()
    sys.exit(0)

config = json.load(open('config.json', 'r'))
gpio = config['pir']['gpio']
GPIO.setmode(GPIO.BOARD)
GPIO.setup(gpio, GPIO.IN)
signal.signal(signal.SIGINT, signalHandler)
while True:
    pir = GPIO.input(gpio)
    if pir == 0:
        print('PIR: waiting for motion...')
        time.sleep(1)
        continue

    print('PIR: motion detected!')
    time.sleep(1)
