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
#   - introduce class, get rid of global variables
#   - check return code of raspistill
#

import RPi.GPIO as GPIO
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
from telegram.error import NetworkError, Unauthorized

# id for keeping track of the last seen message
update_id = None
# config from config file
config = None
# logging stuff
logger = None
# send capture images?
reportMotion = False
# telegram bot
bot = None

def main():
    global config
    global logger
    global update_id
    global bot

    # setup logging, we want to log both to stdout and a file
    logFormat = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    logger = logging.getLogger(__name__)
    fileHandler = logging.handlers.TimedRotatingFileHandler(filename='picam.log', when='D', backupCount=7)
    fileHandler.setFormatter(logFormat)
    logger.addHandler(fileHandler)
    stdoutHandler = logging.StreamHandler(sys.stdout)
    stdoutHandler.setFormatter(logFormat)
    logger.addHandler(stdoutHandler)
    logger.setLevel(logging.INFO)

    logger.info('Starting')

    signal.signal(signal.SIGINT, signalHandler)

    config = json.load(open('config.json', 'r'))
    # check for conflicting config options
    if config['pir']['enable'] and config['motion']['enable']:
        logger.error('Enabling both PIR and motion based capturing is not supported')
        return

    bot = telegram.Bot(config['telegram']['token'])

    # check if API access works. try again on network errors,
    # might happen after boot while the network is still being set up
    logger.info('Waiting for network and API to become accessible...')
    timeout = config['general']['startup_timeout']
    timeout = timeout if timeout > 0 else sys.maxint
    for i in xrange(timeout):
        try:
            logger.info(bot.getMe())
            logger.info('API access working!')
            break # success
        except NetworkError as e:
            pass # don't log, just ignore
        except Exception as e:
            # log other exceptions, then break
            logger.error(e.message)
            logger.error(traceback.format_exc())
            raise
        time.sleep(1)

    # pretend to be nice to our owners
    for owner_id in config['telegram']['owner_ids']:
        try:
            bot.sendMessage(chat_id=owner_id, text='Hello there, I\'m back!')
        except Exception as e:
            # most likely network problem or user has blocked the bot
            logger.warn('Could not send hello to user %s: %s' % (owner_id, e.message))

    # get the first pending update_id, this is so we can skip over it in case
    # we get an "Unauthorized" exception
    try:
        update_id = bot.getUpdates()[0].update_id
    except IndexError:
        update_id = None

    # set up telegram thread
    telegram_thread = threading.Thread(target=fetchTelegramUpdates, args=[bot])
    telegram_thread.daemon = True
    telegram_thread.start()

    # set up watch thread for captured images
    image_watch_thread = threading.Thread(target=fetchImageUpdates, args=[bot])
    image_watch_thread.daemon = True
    image_watch_thread.start()

    # set up PIR thread
    if config['pir']['enable']:
        pir_thread = threading.Thread(target=watchPIR)
        pir_thread.daemon = True
        pir_thread.start()

    while True:
        time.sleep(0.1)
        # TODO XXX FIXME check if all threads are still alive?

def fetchTelegramUpdates(bot):
    logger.info('Setting up telegram thread')
    global update_id
    while True:
        try:
            # request updates after the last update_id
            # timeout: how long to poll for messages
            for update in bot.getUpdates(offset=update_id, timeout=10):
                # chat_id is required to reply to any message
                chat_id = update.message.chat_id
                update_id = update.update_id + 1

                # skip updates without a message
                if not update.message:
                    continue

                message = update.message

                # skip messages from non-owner
                if message.from_user.id not in config['telegram']['owner_ids']:
                    logger.warn('Received message from unknown user "%s": "%s"' % (message.from_user, message.text))
                    message.reply_text("I'm sorry, Dave. I'm afraid I can't do that.")
                    continue

                logger.info('Received message from user "%s": "%s"' % (message.from_user, message.text))
                performCommand(message)
        except NetworkError as e:
            time.sleep(1)
        except Exception as e:
            logger.warn(e.message)
            logger.warn(traceback.format_exc())
            time.sleep(1)

def performCommand(message):
    cmd = message.text.lower().rstrip()
    if cmd == '/start':
        # ignore default start command
        return
    if cmd == '/arm':
        commandArm(message)
    elif cmd == '/disarm':
        commandDisarm(message)
    elif cmd == 'kill':
        commandKill(message)
    elif cmd == '/status':
        commandStatus(message)
    elif cmd == '/capture':
        stopStart = isMotionRunning()
        if stopStart:
            commandDisarm(message)
        commandCapture(message)
        if stopStart:
            commandArm(message)
    else:
        logger.warn('Unknown command: "%s"' % message.text)

def commandArm(message):
    global reportMotion
    if reportMotion:
        message.reply_text('Motion-based capturing already enabled.')
        return

    if not config['motion']['enable'] and not config['pir']['enable']:
        message.reply_text('Error: Cannot enable motion-based capturing since neither PIR nor motion is enabled!')
        return

    message.reply_text('Enabling motion-based capturing...')
    reportMotion = True

    if not config['motion']['enable']:
        # we are done, PIR needs no further steps
        return

    # start motion software if not already running
    if isMotionRunning():
        message.reply_text('Motion software already running.')
        return

    args = shlex.split(config['motion']['cmd'])
    subprocess.call(args)

    # wait until motion is running to prevent
    # multiple start and wrong status reports
    for i in range(10):
        if isMotionRunning():
            message.reply_text('Motion software now running.')
            return
        time.sleep(1)
    message.reply_text('Motion software still not running. Please check status later.')

def commandDisarm(message):
    global reportMotion
    if not reportMotion:
        message.reply_text('Motion-based capturing not enabled.')
        return

    message.reply_text('Disabling motion-based capturing...')
    reportMotion = False 

    if not config['motion']['enable']:
        return

    pid = getMotionPID()
    if pid is None:
        message.reply_text('No PID file found. Assuming motion software not running. If in doubt use "kill".')
        return

    if not os.path.exists('/proc/%s' % pid):
        message.reply_text('PID found but no corresponding proc entry. Removing PID file.')
        os.remove(config['motion']['pid_file'])
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

def commandKill(message):
    if not config['motion']['enable']:
        message.reply_text('Error: kill command only supported when motion is enabled')
        return
    args = shlex.split('killall -9 %s' % config['motion']['kill_name'])
    subprocess.call(args)
    message.reply_text('Kill signal has been sent.')

def commandStatus(message):
    if not reportMotion:
        message.reply_text('Motion-based capturing not enabled.')
        return

    image_dir = config['general']['image_dir']
    if not os.path.exists(image_dir):
        message.reply_text('Error: Motion-based capturing enabled but image dir not available!')
        return
 
    if config['motion']['enable']:
        # check if motion software is running or died unexpectedly
        if not isMotionRunning():
            message.reply_text('Error: Motion-based capturing enabled but motion software not running!')
            return
        message.reply_text('Motion-based capturing enabled and motion software running.')
    else:
        message.reply_text('Motion-based capturing enabled.')

def commandCapture(message):
    message.reply_text('Capture in progress, please wait...')

    capture_file = config['capture']['file'].encode('utf-8')
    if os.path.exists(capture_file):
        os.remove(capture_file)

    args = shlex.split(config['capture']['cmd'])
    subprocess.call(args)

    if not os.path.exists(capture_file):
        message.reply_text('Error: Capture file not found: "%s"' % capture_file)
        return
    
    message.reply_photo(photo=open(capture_file, 'rb'))
    if config['general']['delete_images']:
        os.remove(capture_file)

def fetchImageUpdates(bot):
    logger.info('Setting up image watch thread')

    # set up image directory watch
    watch_dir = config['general']['image_dir']
    # purge (remove and re-create) if we allowed to do so
    if config['general']['delete_images']:
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

        # check for image
        filepath = ('%s/%s' % (watch_path, filename)).encode('utf-8')
        if not filename.endswith('.jpg'):
            logger.info('New non-image file: "%s" - ignored' % filepath)
            continue

        logger.info('New image file: "%s"' % filepath)
        if reportMotion:
            for owner_id in config['telegram']['owner_ids']:
                try:
                    bot.sendPhoto(chat_id=owner_id, caption=filepath, photo=open(filepath, 'rb'))
                except Exception as e:
                    # most likely network problem or user has blocked the bot
                    logger.warn('Could not send image to user %s: %s' % (owner_id, e.message))

        # always delete image, even if reporting is disabled
        if config['general']['delete_images']:
            os.remove(filepath)

def getMotionPID():
    pid_file = config['motion']['pid_file']
    if not os.path.exists(pid_file):
        return None
    with open(pid_file, 'r') as f:
        pid = f.read().rstrip()
    return int(pid)

def isMotionRunning():
    pid = getMotionPID()
    return os.path.exists('/proc/%s' % pid)

def watchPIR():
    logger.info('Setting up PIR watch thread')

    gpio = config['pir']['gpio']
    GPIO.setmode(GPIO.BOARD)
    GPIO.setup(gpio, GPIO.IN)
    while True:
        pir = GPIO.input(gpio)
        if pir == 0:
            # no motion detected
            time.sleep(1)
            continue

        if not reportMotion:
            time.sleep(1)
            continue

        logger.info('PIR: motion detected')
        args = shlex.split(config['pir']['capture_cmd'])
        subprocess.call(args)

def signalHandler(signal, frame):
    global bot

    logger.error('Caught signal %d, terminating now.', signal)
    for owner_id in config['telegram']['owner_ids']:
        try:
            bot.sendMessage(chat_id=owner_id, text='Caught signal %d, terminating now.' % signal)
        except Exception as e:
            # most likely network problem or user has blocked the bot
            logger.warn('Could not send hello to user %s: %s' % (owner_id, e.message))
    sys.exit(1)

if __name__ == '__main__':
    main()
