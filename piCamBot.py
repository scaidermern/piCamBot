#!/usr/bin/env python
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
import shlex
import shutil
import signal
import subprocess
import sys
import telegram
import threading
import time
import traceback
from six.moves import range
from telegram.error import NetworkError, Unauthorized

class piCamBot:
    def __init__(self):
        # id for keeping track of the last seen message
        self.update_id = None
        # config from config file
        self.config = None
        # logging stuff
        self.logger = None
        # check for motion and send captured images to owners?
        self.armed = False
        # telegram bot
        self.bot = None
        # GPIO module, dynamically loaded depending on config
        self.GPIO = None

    def run(self):
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

        try:
            self.config = json.load(open('config.json', 'r'))
        except Exception as e:
            self.logger.error(str(e))
            self.logger.error(traceback.format_exc())
            self.logger.error("Could not parse config file")
            sys.exit(1)
        # check for conflicting config options
        if self.config['pir']['enable'] and self.config['motion']['enable']:
            self.logger.error('Enabling both PIR and motion based capturing is not supported')
            sys.exit(1)

        # check if we need GPIO support
        if self.config['buzzer']['enable'] or self.config['pir']['enable']:
            self.GPIO = importlib.import_module('RPi.GPIO')

        # register signal handler, needs config to be initialized
        signal.signal(signal.SIGHUP, self.signalHandler)
        signal.signal(signal.SIGINT, self.signalHandler)
        signal.signal(signal.SIGQUIT, self.signalHandler)
        signal.signal(signal.SIGTERM, self.signalHandler)

        # set default state
        self.armed = self.config['general']['arm']

        self.bot = telegram.Bot(self.config['telegram']['token'])

        # check if API access works. try again on network errors,
        # might happen after boot while the network is still being set up
        self.logger.info('Waiting for network and API to become accessible...')
        timeout = self.config['general']['startup_timeout']
        timeout = timeout if timeout > 0 else sys.maxsize
        for i in range(timeout):
            try:
                self.logger.info(self.bot.getMe())
                self.logger.info('API access working!')
                break # success
            except NetworkError as e:
                pass # don't log, just ignore
            except Exception as e:
                # log other exceptions, then break
                self.logger.error(str(e))
                self.logger.error(traceback.format_exc())
                raise
            time.sleep(1)

        # pretend to be nice to our owners
        for owner_id in self.config['telegram']['owner_ids']:
            try:
                self.bot.sendMessage(chat_id=owner_id, text='Hello there, I\'m back!')
            except Exception as e:
                # most likely network problem or user has blocked the bot
                self.logger.warn('Could not send hello to user %s: %s' % (owner_id, str(e)))

        # get the first pending update_id, this is so we can skip over it in case
        # we get an "Unauthorized" exception
        try:
            self.update_id = self.bot.getUpdates()[0].update_id
        except IndexError:
            self.update_id = None

        # set up buzzer if configured
        if self.config['buzzer']['enable']:
            gpio = self.config['buzzer']['gpio']
            self.GPIO.setmode(self.GPIO.BOARD)
            self.GPIO.setup(gpio, self.GPIO.OUT)

        threads = []

        # set up telegram thread
        telegram_thread = threading.Thread(target=self.fetchTelegramUpdates, name="Telegram")
        telegram_thread.daemon = True
        telegram_thread.start()
        threads.append(telegram_thread)

        # set up watch thread for captured images
        image_watch_thread = threading.Thread(target=self.fetchImageUpdates, name="Image watch")
        image_watch_thread.daemon = True
        image_watch_thread.start()
        threads.append(image_watch_thread)

        # set up PIR thread
        if self.config['pir']['enable']:
            pir_thread = threading.Thread(target=self.watchPIR, name="PIR")
            pir_thread.daemon = True
            pir_thread.start()
            threads.append(pir_thread)

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
                        self.bot.sendMessage(chat_id=owner_id, text=msg)
                    except Exception as e:
                        pass
                sys.exit(1)

    def fetchTelegramUpdates(self):
        self.logger.info('Setting up telegram thread')
        while True:
            try:
                # request updates after the last update_id
                # timeout: how long to poll for messages
                for update in self.bot.getUpdates(offset=self.update_id, timeout=10):
                    # skip updates without a message
                    if not update.message:
                        continue

                    # chat_id is required to reply to any message
                    chat_id = update.message.chat_id
                    self.update_id = update.update_id + 1
                    message = update.message

                    # skip messages from non-owner
                    if message.from_user.id not in self.config['telegram']['owner_ids']:
                        self.logger.warn('Received message from unknown user "%s": "%s"' % (message.from_user, message.text))
                        message.reply_text("I'm sorry, Dave. I'm afraid I can't do that.")
                        continue

                    self.logger.info('Received message from user "%s": "%s"' % (message.from_user, message.text))
                    self.performCommand(message)
            except NetworkError as e:
                time.sleep(1)
            except Exception as e:
                self.logger.warn(str(e))
                self.logger.warn(traceback.format_exc())
                time.sleep(1)

    def performCommand(self, message):
        cmd = message.text.lower().rstrip()
        if cmd == '/start':
            # ignore default start command
            return
        if cmd == '/arm':
            self.commandArm(message)
        elif cmd == '/disarm':
            self.commandDisarm(message)
        elif cmd == 'kill':
            self.commandKill(message)
        elif cmd == '/status':
            self.commandStatus(message)
        elif cmd == '/capture':
            # if motion software is running we have to stop and restart it for capturing images
            stopStart = self.isMotionRunning()
            if stopStart:
                self.commandDisarm(message)
            self.commandCapture(message)
            if stopStart:
                self.commandArm(message)
        else:
            self.logger.warn('Unknown command: "%s"' % message.text)

    def commandArm(self, message):
        if self.armed:
            message.reply_text('Motion-based capturing already enabled! Nothing to do.')
            return

        if not self.config['motion']['enable'] and not self.config['pir']['enable']:
            message.reply_text('Error: Cannot enable motion-based capturing since neither PIR nor motion is enabled!')
            return

        message.reply_text('Enabling motion-based capturing...')

        if self.config['buzzer']['enable']:
            buzzer_sequence = self.config['buzzer']['seq_arm']
            if len(buzzer_sequence) > 0:
                self.playSequence(buzzer_sequence)

        self.armed = True

        if not self.config['motion']['enable']:
            # we are done, PIR-mode needs no further steps
            return

        # start motion software if not already running
        if self.isMotionRunning():
            message.reply_text('Motion software already running.')
            return

        args = shlex.split(self.config['motion']['cmd'])
        try:
            subprocess.call(args)
        except Exception as e:
            self.logger.warn(str(e))
            self.logger.warn(traceback.format_exc())
            message.reply_text('Error: Failed to start motion software: %s' % str(e))
            return

        # wait until motion is running to prevent
        # multiple start and wrong status reports
        for i in range(10):
            if self.isMotionRunning():
                message.reply_text('Motion software now running.')
                return
            time.sleep(1)
        message.reply_text('Motion software still not running. Please check status later.')

    def commandDisarm(self, message):
        if not self.armed:
            message.reply_text('Motion-based capturing not enabled! Nothing to do.')
            return

        message.reply_text('Disabling motion-based capturing...')

        if self.config['buzzer']['enable']:
            buzzer_sequence = self.config['buzzer']['seq_disarm']
            if len(buzzer_sequence) > 0:
                self.playSequence(buzzer_sequence)

        self.armed = False

        if not self.config['motion']['enable']:
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

    def commandKill(self, message):
        if not self.config['motion']['enable']:
            message.reply_text('Error: kill command only supported when motion is enabled')
            return
        args = shlex.split('killall -9 %s' % self.config['motion']['kill_name'])
        try:
            subprocess.call(args)
        except Exception as e:
            self.logger.warn(str(e))
            self.logger.warn(traceback.format_exc())
            message.reply_text('Error: Failed to send kill signal: %s' % str(e))
            return
        message.reply_text('Kill signal has been sent.')

    def commandStatus(self, message):
        if not self.armed:
            message.reply_text('Motion-based capturing not enabled.')
            return

        image_dir = self.config['general']['image_dir']
        if not os.path.exists(image_dir):
            message.reply_text('Error: Motion-based capturing enabled but image dir not available!')
            return
     
        if self.config['motion']['enable']:
            # check if motion software is running or died unexpectedly
            if not self.isMotionRunning():
                message.reply_text('Error: Motion-based capturing enabled but motion software not running!')
                return
            message.reply_text('Motion-based capturing enabled and motion software running.')
        else:
            message.reply_text('Motion-based capturing enabled.')

    def commandCapture(self, message):
        message.reply_text('Capture in progress, please wait...')

        if self.config['buzzer']['enable']:
            buzzer_sequence = self.config['buzzer']['seq_capture']
            if len(buzzer_sequence) > 0:
                self.playSequence(buzzer_sequence)

        capture_file = self.config['capture']['file']
        if sys.version_info[0] == 2: # yay! python 2 vs 3 unicode fuckup
            capture_file = capture_file.encode('utf-8')
        if os.path.exists(capture_file):
            os.remove(capture_file)

        args = shlex.split(self.config['capture']['cmd'])
        try:
            subprocess.call(args)
        except Exception as e:
            self.logger.warn(str(e))
            self.logger.warn(traceback.format_exc())
            message.reply_text('Error: Capture failed: %s' % str(e))
            return

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
        notify.add_watch(watch_dir.encode('utf-8'))

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

            # check for image
            if sys.version_info[0] == 3: # yay! python 2 vs 3 unicode fuckup
                watch_path = watch_path.decode()
                filename = filename.decode()
            filepath = ('%s/%s' % (watch_path, filename))

            if not filename.endswith('.jpg'):
                self.logger.info('New non-image file: "%s" - ignored' % filepath)
                continue

            self.logger.info('New image file: "%s"' % filepath)
            if self.armed:
                for owner_id in self.config['telegram']['owner_ids']:
                    try:
                        self.bot.sendPhoto(chat_id=owner_id, caption=filepath, photo=open(filepath, 'rb'))
                    except Exception as e:
                        # most likely network problem or user has blocked the bot
                        self.logger.warn('Could not send image to user %s: %s' % (owner_id, str(e)))

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

        if self.config['buzzer']['enable']:
            buzzer_sequence = self.config['buzzer']['seq_motion']

        gpio = self.config['pir']['gpio']
        self.GPIO.setmode(self.GPIO.BOARD)
        self.GPIO.setup(gpio, self.GPIO.IN)
        while True:
            if not self.armed:
                # motion detection currently disabled
                time.sleep(1)
                continue

            pir = self.GPIO.input(gpio)
            if pir == 0:
                # no motion detected
                time.sleep(1)
                continue

            self.logger.info('PIR: motion detected')
            if self.config['buzzer']['enable'] and len(buzzer_sequence) > 0:
                self.playSequence(buzzer_sequence)
            args = shlex.split(self.config['pir']['capture_cmd'])

            try:
                subprocess.call(args)
            except Exception as e:
                self.logger.warn(str(e))
                self.logger.warn(traceback.format_exc())
                message.reply_text('Error: Capture failed: %s' % str(e))

    def playSequence(self, sequence):
        gpio = self.config['buzzer']['gpio']
        duration = self.config['buzzer']['duration']
        for i in sequence:
            if i == '1':
                self.GPIO.output(gpio, 1)
            elif i == '0':
                self.GPIO.output(gpio, 0)
            else:
                self.logger.warnprint('unknown pattern in sequence: %s', i)
            time.sleep(duration)
        self.GPIO.output(gpio, 0)

    def signalHandler(self, signal, frame):
        # always disable buzzer
        if self.config['buzzer']['enable']:
            gpio = self.config['buzzer']['gpio']
            self.GPIO.output(gpio, 0)
            self.GPIO.cleanup()

        msg = 'Caught signal %d, terminating now.' % signal
        self.logger.error(msg)
        for owner_id in self.config['telegram']['owner_ids']:
            try:
                self.bot.sendMessage(chat_id=owner_id, text=msg)
            except Exception as e:
                pass
        sys.exit(1)

if __name__ == '__main__':
    bot = piCamBot()
    bot.run()
