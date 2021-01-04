#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import RPi.GPIO as GPIO
import json
import signal
import sys
import time

# always cleanup GPIO on strg-c or kill
# and disable LED
def signalHandler(signal, frame):
    print('LED: off')
    GPIO.output(gpio, 0)
    GPIO.cleanup()
    sys.exit(0)

config = json.load(open('config.json', 'r'))
gpio = config['capture']['led']['gpio']
GPIO.setmode(GPIO.BCM)
GPIO.setup(gpio, GPIO.OUT)
signal.signal(signal.SIGINT, signalHandler)
while True:
    print('LED: on')
    GPIO.output(gpio, 1)
    time.sleep(1)
    print('LED: off')
    GPIO.output(gpio, 0)
    time.sleep(1)
