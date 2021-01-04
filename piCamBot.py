#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# dependencies:
# - https://github.com/python-telegram-bot/python-telegram-bot
# - https://github.com/dsoprea/PyInotify
#
# similar project:
# - https://github.com/FutureSharks/rpi-security/blob/master/bin/rpi-security.py
#
# - todo:
#   - configurable log file path
#   - check return code of raspistill
#

import importlib
import inotify.adapters
import json
import logging
import logging.handlers
import os
import queue
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from telegram.error import NetworkError, Unauthorized
from telegram.ext import Updater, MessageHandler, Filters

class piCamBot:
    def __init__(self):
        # config from config file
        self.config = None
        # logging stuff
        self.logger = None
        # check for motion and send captured images to owners?
        self.armed = False
        # telegram bot updater
        self.updater = None
        # movement detection via PIR enabled?
        self.pir = False
        # movement detection via motion software enabled?
        self.motion = False
        # buzzer enabled?
        self.buzzer = False
        # queue of sequences to play via buzzer
        self.buzzerQueue = None
        # turn on LED(s) during image capture?
        self.captureLED = False
        # GPIO output for capture LED(s)
        self.captureLEDgpio = None
        # GPIO module, dynamically loaded depending on config
        self.GPIO = None
        # are we currently shutting down?
        self.shuttingDown = False

    def run(self):
        try:
            self.runInternal()
        finally:
            self.cleanup()

    def runInternal(self):
        # setup logging, we want to log both to stdout and a file
        logFormat = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        self.logger = logging.getLogger(__name__)
        fileHandler = logging.handlers.TimedRotatingFileHandler(filename='picam.log', when='D', backupCount=7)
        fileHandler.setFormatter(logFormat)
        self.logger.addHandler(fileHandler)
        stdoutHandler = logging.StreamHandler(sys.stdout)
        stdoutHandler.setFormatter(logFormat)
        self.logger.addHandler(stdoutHandler)
        self.logger.setLevel(logging.INFO)

        self.logger.info('Starting')

        # register signal handler, needs config to be initialized
        signal.signal(signal.SIGHUP, self.signalHandler)
        signal.signal(signal.SIGINT, self.signalHandler)
        signal.signal(signal.SIGQUIT, self.signalHandler)
        signal.signal(signal.SIGTERM, self.signalHandler)

        try:
            self.config = json.load(open('config.json', 'r'))
        except:
            self.logger.exception('Could not parse config file:')
            sys.exit(1)

        self.pir = self.config['pir']['enable']
        self.motion = self.config['motion']['enable']
        self.buzzer = self.config['buzzer']['enable']
        self.captureLED = self.config['capture']['led']['enable']

        # check for conflicting config options
        if self.pir and self.motion:
            self.logger.error('Enabling both PIR and motion based capturing is not supported')
            sys.exit(1)

        # check if we need GPIO support
        if self.buzzer or self.pir or self.captureLED:
            self.GPIO = importlib.import_module('RPi.GPIO')
            self.GPIO.setmode(self.GPIO.BCM)

        if self.captureLED:
            self.captureLEDgpio = self.config['capture']['led']['gpio']
            self.GPIO.setup(self.captureLEDgpio, self.GPIO.OUT)

        # set default state
        self.armed = self.config['general']['arm']

        self.updater = Updater(self.config['telegram']['token'])
        dispatcher = self.updater.dispatcher
        bot = self.updater.bot

        # check if API access works. try again on network errors,
        # might happen after boot while the network is still being set up
        self.logger.info('Waiting for network and Telegram API to become accessible...')
        telegramAccess = False
        timeout = self.config['general']['startup_timeout']
        timeout = timeout if timeout > 0 else sys.maxsize
        for i in range(timeout):
            try:
                self.logger.info(bot.get_me())
                self.logger.info('Telegram API access working!')
                telegramAccess = True
                break # success
            except NetworkError as e:
                pass # don't log network errors, just ignore
            except Unauthorized as e:
                # probably wrong access token
                self.logger.exception('Error while trying to access Telegram API, wrong Telegram access token?')
                raise
            except:
                # unknown exception, log and then bail out
                self.logger.exception('Error while trying to access Telegram API:')
                raise

            time.sleep(1)

        if not telegramAccess:
            self.logger.error('Could not access Telegram API within time, shutting down')
            sys.exit(1)

        # pretend to be nice to our owners
        for owner_id in self.config['telegram']['owner_ids']:
            try:
                bot.sendMessage(chat_id=owner_id, text='Hello there, I\'m back!')
            except:
                # most likely network problem or user has blocked the bot
                self.logger.exception('Could not send hello to user %s:' % owner_id)


        threads = []

        # set up watch thread for captured images
        image_watch_thread = threading.Thread(target=self.fetchImageUpdates, name="Image watch")
        image_watch_thread.daemon = True
        image_watch_thread.start()
        threads.append(image_watch_thread)

        # set up PIR thread
        if self.pir:
            pir_thread = threading.Thread(target=self.watchPIR, name="PIR")
            pir_thread.daemon = True
            pir_thread.start()
            threads.append(pir_thread)

        # set up buzzer thread
        if self.buzzer:
            buzzer_thread = threading.Thread(target=self.watchBuzzerQueue, name="buzzer")
            buzzer_thread.daemon = True
            buzzer_thread.start()
            threads.append(buzzer_thread)

        # register message handler and start polling
        # note: we don't register each command individually because then we
        # wouldn't be able to check the owner_id, instead we register for text
        # messages
        dispatcher.add_handler(MessageHandler(Filters.text, self.performCommand))
        self.updater.start_polling()

        while True:
            time.sleep(1)
            # check if all threads are still alive
            for thread in threads:
                if thread.isAlive():
                    continue

                # something went wrong, bailing out
                msg = 'Thread "%s" died, terminating now.' % thread.name
                self.logger.error(msg)
                for owner_id in self.config['telegram']['owner_ids']:
                    try:
                        bot.sendMessage(chat_id=owner_id, text=msg)
                    except:
                        self.logger.exception('Exception while trying to notify owners:')
                        pass
                sys.exit(1)

    def performCommand(self, update, context):
        message = update.message
        # skip messages from non-owner
        if message.from_user.id not in self.config['telegram']['owner_ids']:
            self.logger.warning('Received message from unknown user "%s": "%s"' % (message.from_user, message.text))
            message.reply_text("I'm sorry, Dave. I'm afraid I can't do that.")
            return

        self.logger.info('Received message from user "%s": "%s"' % (message.from_user, message.text))

        cmd = update.message.text.lower().rstrip()
        if cmd == '/start':
            # ignore default start command
            return
        if cmd == '/arm':
            self.commandArm(update)
        elif cmd == '/disarm':
            self.commandDisarm(update)
        elif cmd == '/kill':
            self.commandKill(update)
        elif cmd == '/status':
            self.commandStatus(update)
        elif cmd == '/capture':
            # if motion software is running we have to stop and restart it for capturing images
            stopStart = self.isMotionRunning()
            if stopStart:
                self.commandDisarm(update)
            self.commandCapture(update)
            if stopStart:
                self.commandArm(update)
        else:
            self.logger.warning('Unknown command: "%s"' % update.message.text)

    def commandArm(self, update):
        message = update.message
        if self.armed:
            message.reply_text('Motion-based capturing already enabled! Nothing to do.')
            return

        if not self.pir and not self.motion:
            message.reply_text('Error: Cannot enable motion-based capturing since neither PIR nor motion is enabled!')
            return

        message.reply_text('Enabling motion-based capturing...')

        if self.buzzer:
            sequence = self.config['buzzer']['seq_arm']
            if len(sequence) > 0:
                self.buzzerQueue.put(sequence)

        self.armed = True

        if not self.motion:
            # we are done, PIR-mode needs no further steps
            return

        # start motion software if not already running
        if self.isMotionRunning():
            message.reply_text('Motion software already running.')
            return

        args = shlex.split(self.config['motion']['cmd'])
        try:
            subprocess.call(args)
        except:
            self.logger.exception('Failed to start motion software:')
            message.reply_text('Error: Failed to start motion software. See log for details.')
            return

        # wait until motion is running to prevent
        # multiple start and wrong status reports
        for i in range(10):
            if self.isMotionRunning():
                message.reply_text('Motion software now running.')
                return
            time.sleep(1)
        message.reply_text('Motion software still not running. Please check status later.')

    def commandDisarm(self, update):
        message = update.message
        if not self.armed:
            message.reply_text('Motion-based capturing not enabled! Nothing to do.')
            return

        message.reply_text('Disabling motion-based capturing...')

        if self.buzzer:
            sequence = self.config['buzzer']['seq_disarm']
            if len(sequence) > 0:
                self.buzzerQueue.put(sequence)

        self.armed = False

        if not self.motion:
            # we are done, PIR-mode needs no further steps
            return

        pid = self.getMotionPID()
        if pid is None:
            message.reply_text('No PID file found. Assuming motion software not running. If in doubt use "kill".')
            return

        if not os.path.exists('/proc/%s' % pid):
            message.reply_text('PID found but no corresponding proc entry. Removing PID file.')
            os.remove(self.config['motion']['pid_file'])
            return

        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            # ingore if already gone
            pass
        # wait for process to terminate, can take some time
        for i in range(10):
            if not os.path.exists('/proc/%s' % pid):
                message.reply_text('Motion software has been stopped.')
                return
            time.sleep(1)
        
        message.reply_text("Could not terminate process. Trying to kill it...")
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            # ignore if already gone
            pass

        # wait for process to terminate, can take some time
        for i in range(10):
            if not os.path.exists('/proc/%s' % pid):
                message.reply_text('Motion software has been stopped.')
                return
            time.sleep(1)
        message.reply_text('Error: Unable to stop motion software.')

    def commandKill(self, update):
        message = update.message
        if not self.motion:
            message.reply_text('Error: kill command only supported when motion is enabled')
            return
        args = shlex.split('killall -9 %s' % self.config['motion']['kill_name'])
        try:
            subprocess.call(args)
        except:
            self.logger.exception('Failed to send kill signal:')
            message.reply_text('Error: Failed to send kill signal. See log for details.')
            return
        message.reply_text('Kill signal has been sent.')

    def commandStatus(self, update):
        message = update.message
        if not self.armed:
            message.reply_text('Motion-based capturing not enabled.')
            return

        image_dir = self.config['general']['image_dir']
        if not os.path.exists(image_dir):
            message.reply_text('Error: Motion-based capturing enabled but image dir not available!')
            return
     
        if self.motion:
            # check if motion software is running or died unexpectedly
            if not self.isMotionRunning():
                message.reply_text('Error: Motion-based capturing enabled but motion software not running!')
                return
            message.reply_text('Motion-based capturing enabled and motion software running.')
        else:
            message.reply_text('Motion-based capturing enabled.')

    def commandCapture(self, update):
        message = update.message
        message.reply_text('Capture in progress, please wait...')

        # enable capture LED(s)
        if self.captureLED:
            self.GPIO.output(self.captureLEDgpio, 1)

        # enqueue buzzer sequence
        if self.buzzer:
            sequence = self.config['buzzer']['seq_capture']
            if len(sequence) > 0:
                self.buzzerQueue.put(sequence)

        capture_file = self.config['capture']['file']
        if os.path.exists(capture_file):
            os.remove(capture_file)

        args = shlex.split(self.config['capture']['cmd'])
        try:
            subprocess.call(args)
        except:
            self.logger.exception('Capture failed:')
            message.reply_text('Error: Capture failed. See log for details.')
            return
        finally:
            # always disable capture LEDs
            self.GPIO.output(self.captureLEDgpio, 0)

        if not os.path.exists(capture_file):
            message.reply_text('Error: Capture file not found: "%s"' % capture_file)
            return
        
        message.reply_photo(photo=open(capture_file, 'rb'))
        if self.config['general']['delete_images']:
            os.remove(capture_file)

    def fetchImageUpdates(self):
        self.logger.info('Setting up image watch thread')

        # set up image directory watch
        watch_dir = self.config['general']['image_dir']
        # purge (remove and re-create) if we allowed to do so
        if self.config['general']['delete_images']:
            shutil.rmtree(watch_dir, ignore_errors=True)
        if not os.path.exists(watch_dir):
            os.makedirs(watch_dir) # racy but we don't care
        notify = inotify.adapters.Inotify()
        notify.add_watch(watch_dir)

        # check for new events
        # (runs forever but we could bail out: check for event being None
        #  which always indicates the last event)
        for event in notify.event_gen():
            if event is None:
                continue

            (header, type_names, watch_path, filename) = event

            # only watch for created and renamed files
            matched_types = ['IN_CLOSE_WRITE', 'IN_MOVED_TO']
            if not any(type in type_names for type in matched_types):
                continue

            filepath = ('%s/%s' % (watch_path, filename))

            if not filename.endswith('.jpg'):
                self.logger.info('New non-image file: "%s" - ignored' % filepath)
                continue

            self.logger.info('New image file: "%s"' % filepath)
            if self.armed:
                bot = self.updater.dispatcher.bot
                for owner_id in self.config['telegram']['owner_ids']:
                    try:
                        bot.sendPhoto(chat_id=owner_id, caption=filepath, photo=open(filepath, 'rb'))
                    except:
                        # most likely network problem or user has blocked the bot
                        self.logger.exception('Could not send image to user %s: %s' % owner_id)

            # always delete image, even if reporting is disabled
            if self.config['general']['delete_images']:
                os.remove(filepath)

    def getMotionPID(self):
        pid_file = self.config['motion']['pid_file']
        if not os.path.exists(pid_file):
            return None
        with open(pid_file, 'r') as f:
            pid = f.read().rstrip()
        return int(pid)

    def isMotionRunning(self):
        pid = self.getMotionPID()
        return os.path.exists('/proc/%s' % pid)

    def watchPIR(self):
        self.logger.info('Setting up PIR watch thread')

        sequence = None
        if self.buzzer:
            sequence = self.config['buzzer']['seq_motion']
            if len(sequence) == 0:
                sequence = None

        gpio = self.config['pir']['gpio']
        self.GPIO.setup(gpio, self.GPIO.IN)
        while True:
            if not self.armed:
                # motion detection currently disabled
                time.sleep(0.1)
                continue

            pir = self.GPIO.input(gpio)
            if pir == 0:
                # no motion detected
                time.sleep(0.1)
                continue

            self.logger.info('PIR: motion detected')
            if sequence:
                self.buzzerQueue.put(sequence)
            args = shlex.split(self.config['pir']['capture_cmd'])

            try:
                subprocess.call(args)
            except:
                self.logger.exception('Error: Capture failed:')
                message.reply_text('Error: Capture failed. See log for details.')

    def watchBuzzerQueue(self):
        self.logger.info('Setting up buzzer thread')

        gpio = self.config['buzzer']['gpio']
        self.GPIO.setup(gpio, self.GPIO.OUT)

        duration = self.config['buzzer']['duration']

        self.buzzerQueue = queue.SimpleQueue()

        # play arm sequence if we are armed right on startup
        if self.armed:
            sequence = self.config['buzzer']['seq_arm']
            if len(sequence) > 0:
                self.buzzerQueue.put(sequence)

        while True:
            # wait for queued items and play them
            sequence = self.buzzerQueue.get(block=True, timeout=None)
            self.playSequence(sequence, duration, gpio)

    def playSequence(self, sequence, duration, gpio):
        for i in sequence:
            if i == '1':
                self.GPIO.output(gpio, 1)
            elif i == '0':
                self.GPIO.output(gpio, 0)
            else:
                self.logger.warning('unknown pattern in sequence: %s', i)
            time.sleep(duration)
        self.GPIO.output(gpio, 0)

    def cleanup(self):
        if self.buzzer:
            try:
                self.logger.info('Disabling buzzer')
                gpio = self.config['buzzer']['gpio']
                self.GPIO.output(gpio, 0)
            except:
                pass

        if self.captureLED:
            try:
                self.logger.info('Disabling capture LED(s)')
                self.GPIO.output(self.captureLEDgpio, 0)
            except:
                pass

        if self.GPIO is not None:
            try:
                self.logger.info('Cleaning up GPIO')
                self.GPIO.cleanup()
            except:
                pass

        if self.updater and self.updater.running:
            try:
                self.logger.info('Stopping telegram updater')
                self.updater.stop()
            except:
                pass

        self.logger.info('Cleanup done')

    def signalHandler(self, signal, frame):
        # prevent multiple calls by different signals (e.g. SIGHUP, then SIGTERM)
        if self.shuttingDown:
            return
        self.shuttingDown = True

        msg = 'Caught signal %d, terminating now.' % signal
        self.logger.error(msg)

        # try to inform owners
        if self.updater and self.updater.running:
            try:
                bot = self.updater.dispatcher.bot
                for owner_id in self.config['telegram']['owner_ids']:
                    try:
                        bot.sendMessage(chat_id=owner_id, text=msg)
                    except:
                        pass
            except:
                pass

        sys.exit(1)

if __name__ == '__main__':
    bot = piCamBot()
    bot.run()
