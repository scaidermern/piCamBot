# piCamBot
Security camera based on a Raspberry Pi and Telegram, controllable by smartphone.

## Description
This is a simple Telegram bot that acts as a security camera. It is intented to run on a Raspberry Pi but may be used on any other Linux system, too. It requires a camera (for example a Raspberry Pi Camera Module v2) and either a PIR sensor or the software *motion*.

## Requirements
- Raspberry PI (or any other Linux system)
- Camera (e.g. Raspberry Pi Camera Module v2)
- PIR sensor (e.g. HC-SR501) or [motion](http://lavrsen.dk/foswiki/bin/view/Motion/WebHome) software (using the PIR sensor is recommended, it works way better than using motion software)
- [Telegram](https://telegram.org/) account and a [Telegram bot](https://core.telegram.org/bots)
- python:
  - [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot)
  - [PyInotify](https://github.com/dsoprea/PyInotify)
  
## Setup

### 1) Configuration
Edit `config.json`. Enter your Telegram `token` and `owner_ids`. See these [instructions for obtaining your Telegram user ID](https://stackoverflow.com/questions/31078710/how-to-obtain-telegram-chat-id-for-a-specific-user). Alternatively just run piCamBot and send a message to your bot. piCamBot will log messages from unknown users and write out their user IDs.

If you aren't using a Raspberry Pi then you need to change `pir`:`capture_cmd` and `capture`:`cmd` to use a different command than `raspistill`.

Either enable `pir` (when using a PIR sensor) or `motion` (when no PIR sensor is available). It is highly recommended to use a PIR sensor since it works better than motion in my experience.

Note: you can't enable `pir` and `motion` at the same time. However you can disable both and still use piCamBot to perform manual camera captures.

#### 1a) Using a PIR sensor
Set a correct `pir`:`gpio` port. You can use `python pir_test.py` to check if the PIR is working and a correct gpio port has been configured.

#### 1b) Using motion
Check that the `pid_file` path is correct. It must match the `process_id_file` setting in your `motion.conf`. Also check that `general`:`image_dir` matches your `motion.conf`'s `target_dir`. Edit `motion.conf` and adjust `rotate`, `width`, `height` to your camera. Also adjust `threshold` and `noise_level` to your environment (good luck with that...). `daemon` mode must be enabled for piCamBot!

Ideally run motion separately to adjust all these settings until it matches your expected results. Afterwards try to use it with piCamBot.

### 2) Starting the bot
Execute `python bot.py`. The bot will automatically send a greeting message to all owners if Telegram access is working.

### 3) Controlling the bot
The bot will start with motion-based capturing being disabled.

After enabing motion-based capturing it will either react on the PIR sensor and performs captures whenever a motion is reported. Or it reacts on captures performed by the motion software. In either case, captured images are sent via Telegram to all owners.

It supports the following commands:
- `/arm`: Starts motion-based capturing. If `motion` software is enabled it will be started as well.
- `/disarm`: Stops motion-based capturing. If `motion` software is enabled it will be stopped as well.
- `/status`: Reports whether motion-based capturing is currently enabled.
- `/capture`: Takes a manual capture with the camera. If motion-based capturing and `motion` software is enabled it will be temporarily stopped and started again after the capture. This is needed since access to the camera is exclusive.
- `/kill`: Only to be used if motion` software is enabled. This kills the software (using SIGKILL) in case it is running and `/disarm` fails to stop it.

# License
[GPL v3](http://www.gnu.org/licenses/gpl.html)
(c) [Alexander Heinlein](http://choerbaert.org)
