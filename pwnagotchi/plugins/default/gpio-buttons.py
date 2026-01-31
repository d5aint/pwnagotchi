import logging
import subprocess
import RPi.GPIO as GPIO
import pwnagotchi.plugins as plugins


class GPIOButtons(plugins.Plugin):
    __author__ = 'ratmandu@gmail.com'
    __version__ = '1.0.0'
    __license__ = 'GPL3'
    __description__ = 'GPIO Button support plugin'
    __defaults__ = {
        'gpios': {
            '5': 'touch /root/test_button',
            '6': 'echo "Hello" > /root/hello.txt'
        }
    }

    def __init__(self):
        self.running = False
        self.ports = {}
        self.commands = None
        self.options = dict()

    def run_command(self, channel):
        command = self.ports[channel]
        logging.info(f"[gpio] Button on GPIO {channel} pressed! Running: {command}")
        try:
            process = subprocess.Popen(
                command,
                shell=True,
                stdin=None,
                stdout=open("/dev/null", "w"),
                stderr=None,
                executable="/bin/bash"
            )
            process.wait()
        except Exception as e:
            logging.error(f"[gpio] Error running command: {e}")

    def on_loaded(self):
        logging.info("[gpio] GPIO Button plugin loaded.")

        if 'gpios' not in self.options:
            logging.warning("[gpio] No buttons configured.")
            return

        # Use BCM numbering
        GPIO.setmode(GPIO.BCM)

        # Set specific LED pins for Pimoroni Display HAT Mini (optional/specific hardware)
        # 17, 22, 27 are RGB LEDs on that specific HAT.
        # Ideally, this should be configurable, but keeping original logic:
        try:
            for pin in [17, 22, 27]:
                GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
        except Exception as e:
            logging.debug(f"[gpio] Failed to setup LED pins: {e}")

        # Setup user-defined buttons
        for gpio_pin, command in self.options['gpios'].items():
            try:
                gpio_int = int(gpio_pin)
                self.ports[gpio_int] = command
                
                GPIO.setup(gpio_int, GPIO.IN, pull_up_down=GPIO.PUD_UP)
                GPIO.add_event_detect(
                    gpio_int, 
                    GPIO.FALLING, 
                    callback=self.run_command, 
                    bouncetime=600
                )
                
                logging.info(f"[gpio] Mapped GPIO {gpio_int} to command: {command}")
            except ValueError:
                logging.error(f"[gpio] Invalid GPIO pin number: {gpio_pin}")
            except Exception as e:
                logging.error(f"[gpio] Failed to setup GPIO {gpio_pin}: {e}")
