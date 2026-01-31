import os
import re
import time
import random
import logging
import subprocess
from io import TextIOWrapper
from threading import Lock

import pwnagotchi
import pwnagotchi.plugins as plugins
import pwnagotchi.ui.faces as faces
import pwnagotchi.ui.fonts as fonts
from pwnagotchi.bettercap import Client
from pwnagotchi.ui.components import Text
from pwnagotchi.ui.view import BLACK


class FixServices(plugins.Plugin):
    __author__ = 'jayofelony'
    __version__ = '1.0.2'
    __license__ = 'GPL3'
    __description__ = 'Fix blindness, firmware crashes and brain not being loaded. Auto-disables for external WiFi adapters.'
    __name__ = 'Fix_Services'
    __help__ = """
    Reload brcmfmac module when blindbug is detected, instead of rebooting. Adapted from WATCHDOG.
    Automatically disables itself when an external WiFi adapter is detected instead of the onboard brcmfmac chip.
    """

    def __init__(self):
        self.options = dict()
        self.lock = Lock()
        
        # Regex patterns for various error states
        self.pattern_iface_err = re.compile(r'ieee80211 phy0: brcmf_cfg80211_add_iface: iface validation failed: err=-95')
        self.pattern_hop_err = re.compile(r'wifi error while hopping to channel')
        self.pattern_fw_crash = re.compile(r'Firmware has halted or crashed')
        self.pattern_no_iface = re.compile(r'error 400: could not find interface wlan0mon')
        self.pattern_map_err = re.compile(r'fatal error: concurrent map iteration and map write')
        self.pattern_panic = re.compile(r'panic: runtime error')
        self.pattern_multicast = re.compile(r'ieee80211 phy0: _brcmf_set_multicast_list: Setting allmulti failed, -110')

        self.is_reloading_mon = False
        self.last_trigger_time = 0
        self.is_disabled = self._check_external_adapter()

    def _check_external_adapter(self):
        """
        Check if an external WiFi adapter is being used instead of the onboard brcmfmac chip.
        Returns True if external adapter detected (plugin should be disabled), False otherwise.
        """
        try:
            # Check if wlan0 interface exists
            if os.path.exists('/sys/class/net/wlan0'):
                try:
                    # Check driver symlink
                    driver_path = "/sys/class/net/wlan0/device/driver"
                    if os.path.exists(driver_path):
                        driver_link = os.readlink(driver_path)
                        driver_name = os.path.basename(driver_link)

                        logging.info(f"[fix-services] Detected WiFi driver: {driver_name}")

                        if driver_name != "brcmfmac":
                            logging.info(f"[fix-services] External WiFi adapter detected ({driver_name}). Plugin disabled.")
                            return True
                        else:
                            logging.info(f"[fix-services] Onboard brcmfmac detected. Plugin active.")
                            return False
                    else:
                        # Fallback to lsmod if sysfs path doesn't exist
                        lsmod_output = subprocess.check_output("lsmod | grep brcmfmac", shell=True, text=True)
                        if lsmod_output.strip():
                            logging.info(f"[fix-services] brcmfmac module detected via lsmod. Plugin active.")
                            return False
                        
                        logging.info(f"[fix-services] brcmfmac module not found. Plugin disabled.")
                        return True

                except subprocess.CalledProcessError:
                    logging.info(f"[fix-services] brcmfmac module not found. Plugin disabled.")
                    return True
                except Exception as e:
                    logging.warning(f"[fix-services] Error checking driver: {e}. Plugin disabled.")
                    return True
            else:
                logging.warning(f"[fix-services] wlan0 interface not found. Plugin disabled.")
                return True

        except Exception as e:
            logging.error(f"[fix-services] Error detecting WiFi adapter: {e}. Plugin disabled.")
            return True

    def _get_face(self, face):
        """Helper to handle faces that might be lists or strings."""
        if isinstance(face, list):
            return random.choice(face)
        return face

    def on_loaded(self):
        if self.is_disabled:
            logging.info(f"[fix-services] Plugin loaded but DISABLED (External Adapter).")
            return
        logging.info(f"[fix-services] Plugin loaded.")

    def on_ready(self, agent):
        if self.is_disabled:
            return

        try:
            cmd_output = subprocess.check_output("ip link show wlan0mon", shell=True, text=True)
            logging.debug(f"[fix-services] ip link show wlan0mon: {cmd_output}")
            if ",UP," in cmd_output:
                logging.debug("wlan0mon is up.")
        except Exception as err:
            logging.error(f"[fix-services] Error checking wlan0mon: {err}")
            try:
                self._restart_driver_stack(agent)
            except Exception as sub_err:
                logging.error(f"[fix-services] Restart failed: {sub_err}")

    def on_bcap_sys_log(self, agent, event):
        """Listen for Bettercap log events indicating hopping errors."""
        if self.is_disabled:
            return

        if re.search('wifi error while hopping to channel', event['data']['Message']):
            logging.debug(f"[fix-services] SYSLOG MATCH: {event['data']['Message']}")
            logging.debug(f"[fix-services] Restarting wifi.recon")
            try:
                result = agent.run("wifi.recon off; wifi.recon on")
                if result.get("success"):
                    logging.debug(f"[fix-services] wifi.recon flip: success!")
                    if hasattr(agent, 'view'):
                        agent.view().update(force=True, new_data={
                            "status": "Wifi recon flipped!",
                            "face": self._get_face(faces.COOL)
                        })
                else:
                    logging.warning(f"[fix-services] wifi.recon flip FAILED: {result}")
                    self._restart_driver_stack(agent)
            except Exception as err:
                logging.error(f"[fix-services] SYSLOG wifi.recon flip fail: {err}")
                self._restart_driver_stack(agent)

    def on_epoch(self, agent, epoch, epoch_data):
        if self.is_disabled:
            return

        # Throttle checks to avoid spamming resets
        if time.time() - self.last_trigger_time < 180:
            return

        try:
            # Read recent kernel logs
            last_lines = ''.join(list(TextIOWrapper(subprocess.Popen(
                ['journalctl', '-n10', '-k'], stdout=subprocess.PIPE).stdout))[-10:])
            
            # Read recent system logs
            sys_last_lines = ''.join(list(TextIOWrapper(subprocess.Popen(
                ['journalctl', '-n10'], stdout=subprocess.PIPE).stdout))[-10:])
            
            # Read Pwnagotchi logs
            pwn_last_lines = ''.join(list(TextIOWrapper(subprocess.Popen(
                ['tail', '-n10', '/etc/pwnagotchi/log/pwnagotchi.log'], stdout=subprocess.PIPE).stdout))[-10:])

            display = agent.view() if hasattr(agent, 'view') else None

            # Pattern 1: Interface Validation Failed
            if self.pattern_iface_err.search(last_lines):
                logging.info(f"[fix-services] Detected iface validation error.")
                subprocess.run("monstop", shell=True, check=False)
                subprocess.run("monstart", shell=True, check=False)
                if display:
                    display.set('status', 'Wifi channel stuck. Restarting recon.')
                    display.update(force=True)
                pwnagotchi.restart("AUTO")

            # Pattern 2: Hopping Error
            elif len(self.pattern_hop_err.findall(sys_last_lines)) >= 5:
                logging.debug(f"[fix-services] Wifi channel stuck. Triggering reload.")
                if display:
                    display.set('status', 'Wifi channel stuck. Restarting recon.')
                    display.update(force=True)

                try:
                    result = agent.run("wifi.recon off; wifi.recon on")
                    if result.get("success"):
                        logging.debug(f"[fix-services] wifi.recon flipped successfully.")
                        if display:
                            display.update(force=True, new_data={
                                "status": "Wifi recon flipped!",
                                "face": self._get_face(faces.COOL)
                            })
                    else:
                        logging.warning(f"[fix-services] wifi.recon flip FAILED.")
                except Exception as e:
                    logging.error(f"[fix-services] Recon flip error: {e}")

            # Pattern 3: Firmware Crashed
            elif self.pattern_fw_crash.search(sys_last_lines):
                logging.warning(f"[fix-services] Firmware crashed. Restarting wlan0mon.")
                if display:
                    display.set('status', 'Firmware halted. Restarting wlan0mon.')
                    display.update(force=True)
                try:
                    subprocess.check_output("monstart", shell=True)
                except Exception as e:
                    logging.error(f"[fix-services] monstart failed: {e}")

            # Pattern 4: wlan0mon Missing
            elif len(self.pattern_no_iface.findall(pwn_last_lines)) >= 3:
                logging.warning(f"[fix-services] wlan0mon missing. Restarting interface.")
                if display:
                    display.set('status', 'Restarting wlan0 now!')
                    display.update(force=True)
                try:
                    subprocess.check_output("monstart", shell=True)
                except Exception as e:
                    logging.error(f"[fix-services] monstart failed: {e}")

            # Pattern 5 & 6: Bettercap/Go Panic
            elif self.pattern_map_err.search(pwn_last_lines) or self.pattern_panic.search(pwn_last_lines):
                logging.critical(f"[fix-services] Bettercap panic detected. Rebooting.")
                if display:
                    display.set('status', 'Critical Error. Restarting...')
                    display.update(force=True)
                os.system("systemctl restart bettercap")
                pwnagotchi.restart("AUTO")

            # Pattern 7: Multicast Error
            elif self.pattern_multicast.search(pwn_last_lines):
                logging.warning(f"[fix-services] Multicast error. Flipping recon.")
                try:
                    agent.run("wifi.recon off; wifi.recon on")
                except Exception as e:
                    logging.error(f"[fix-services] Recon flip error: {e}")

        except Exception as e:
            logging.error(f"[fix-services] Error in on_epoch check: {e}")

    def _log_and_display(self, level, message, ui=None, display_data=None, force=True):
        """Helper to log messages and update UI simultaneously."""
        try:
            if level == "error":
                logging.error(message)
            elif level == "warning":
                logging.warning(message)
            else:
                logging.debug(message)

            if ui and display_data:
                if "face" in display_data:
                    display_data["face"] = self._get_face(display_data["face"])
                ui.update(force=force, new_data=display_data)
            elif display_data and "status" in display_data:
                print(display_data["status"])
            else:
                print(f"[{level}] {message}")
        except Exception as err:
            logging.error(f"[fix-services] Display Error: {err}")

    def _restart_driver_stack(self, connection):
        """
        The core recovery logic:
        1. Pause recon
        2. Stop monitor mode
        3. Unload brcmfmac kernel module
        4. Reload brcmfmac kernel module
        5. Restart monitor mode
        6. Resume recon
        """
        if self.is_disabled:
            return

        # Avoid overlapping restarts
        if self.is_reloading_mon and (time.time() - self.last_trigger_time) < 180:
            logging.debug(f"[fix-services] Duplicate restart attempt ignored.")
            return

        self.is_reloading_mon = True
        self.last_trigger_time = time.time()

        display = connection.view() if hasattr(connection, 'view') else None
        
        if display:
            display.update(force=True, new_data={
                "status": "I'm blind! Power cycling WiFi...",
                "face": self._get_face(faces.BORED)
            })

        # Step 1: Pause Recon
        try:
            result = connection.run("wifi.recon off")
            if result.get("success"):
                self._log_and_display("info", f"[fix-services] wifi.recon paused.", 
                                      display, {"status": "Wifi recon paused!", "face": self._get_face(faces.COOL)})
                time.sleep(2)
            else:
                self._log_and_display("warning", f"[fix-services] wifi.recon pause failed.", 
                                      display, {"status": "Recon broken...", "face": self._get_face(faces.BROKEN)})
        except Exception as e:
            logging.error(f"[fix-services] Error pausing recon: {e}")

        # Step 2: Stop Monitor Interface
        try:
            subprocess.run("monstop", shell=True, check=False)
            self._log_and_display("info", f"[fix-services] wlan0mon stopped.", 
                                  display, {"status": "wlan0mon down", "face": self._get_face(faces.BORED)})
        except Exception:
            pass

        logging.debug(f"[fix-services] Attempting kernel module reload...")

        # Step 3 & 4: Reload Kernel Module (Retries up to 3 times)
        tries = 1
        success = False
        
        while tries < 3:
            try:
                # Unload
                subprocess.check_output("sudo modprobe -r brcmfmac", shell=True)
                self._log_and_display("info", f"[fix-services] Unloaded brcmfmac.", 
                                      display, {"status": f"Turning it off #{tries}", "face": self._get_face(faces.SMART)})
                
                time.sleep(1)

                # Reload
                subprocess.check_output("sudo modprobe brcmfmac", shell=True)
                self._log_and_display("info", f"[fix-services] Reloaded brcmfmac.")

                # Step 5: Start Monitor Interface
                try:
                    subprocess.check_output("monstart", shell=True)
                    
                    # Try setting interface in bettercap
                    result = connection.run("set wifi.interface wlan0mon")
                    if result.get("success"):
                        logging.debug(f"[fix-services] Bettercap interface set successfully.")
                        success = True
                        break
                except Exception as e:
                    logging.error(f"[fix-services] Failed to bring up wlan0mon: {e}")

            except Exception as e:
                logging.error(f"[fix-services] Module reload attempt #{tries} failed: {e}")

            tries += 1
            if tries < 3:
                logging.debug(f"[fix-services] Retrying driver reload...")
            else:
                logging.critical(f"[fix-services] Driver reload failed. Rebooting system.")
                pwnagotchi.reboot()

        # Step 6: Resume Recon
        if success:
            if display:
                display.update(force=True, new_data={
                    "status": "And back on again...",
                    "face": self._get_face(faces.INTENSE)
                })
            
            logging.debug(f"[fix-services] Waiting for interface to settle...")
            self.last_trigger_time = time.time()
            time.sleep(8 + tries * 2)
            self.is_reloading_mon = False

            try:
                result = connection.run("wifi.clear; wifi.recon on")
                if result.get("success"):
                    if display:
                        display.update(force=True, new_data={
                            "status": "I can see again!",
                            "face": self._get_face(faces.HAPPY)
                        })
                    logging.info(f"[fix-services] Recovery successful.")
                    self.last_trigger_time = time.time() + 120
                else:
                    logging.error(f"[fix-services] wifi.recon failed to restart.")
                    self.last_trigger_time = time.time() - 300
            except Exception as e:
                logging.error(f"[fix-services] Error resuming recon: {e}")
                pwnagotchi.reboot()

    def on_ui_setup(self, ui):
        if self.is_disabled:
            return
        # This plugin primarily modifies the status bar, but can accept a position config
        # if you wanted to add a specific visual element later.
        pass

    def on_ui_update(self, ui):
        pass

    def on_unload(self, ui):
        pass