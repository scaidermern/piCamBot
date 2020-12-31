#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import RPi.GPIO as GPIO
import json
import signal
import sys
import time

# always disable buzzer on strg-c or kill
def signalHandler(signal, frame):
    print('buzzer: off')
    GPIO.output(gpio, 0)
    GPIO.cleanup()
    sys.exit(0)

def playSequence(sequence, duration):
    for i in sequence:
        if i == '1':
            GPIO.output(gpio, 1)
            print('buzzer: on')
        elif i == '0':
            GPIO.output(gpio, 0)
            print('buzzer: off')
        else:
            print('unknown pattern in sequence: %s', i)
        time.sleep(duration)

config = json.load(open('config.json', 'r'))
gpio = config['buzzer']['gpio']
duration = config['buzzer']['duration']
sequence = config['buzzer']['seq_motion']
GPIO.setmode(GPIO.BCM)
GPIO.setup(gpio, GPIO.OUT)
signal.signal(signal.SIGINT, signalHandler)
while True:
    playSequence(sequence, duration)
