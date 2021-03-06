#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import RPi.GPIO as GPIO
import json
import signal
import sys
import time

# always cleanup GPIO on strg-c or kill
def signalHandler(signal, frame):
    GPIO.cleanup()
    sys.exit(0)

config = json.load(open('config.json', 'r'))
gpio = config['pir']['gpio']
GPIO.setmode(GPIO.BCM)
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
