import logging
import json
import os
import re
import pathlib
from datetime import datetime, timezone
from threading import Lock

import pwnagotchi.plugins as plugins


def read_ap_cache(cache_dir, filename):
    """
    Reads the cached AP JSON data for a specific capture file.
    Expects the filename to be a pcap or json file, converts the extension
    to the expected cache format.
    """
    base_name = os.path.basename(filename)
    # Convert extensions like .pcap to .Cache as per original logic
    cache_filename = re.sub(r"\.(pcap|gps\.json|geo\.json)$", ".Cache", base_name)
    cache_filepath = os.path.join(cache_dir, cache_filename)

    if not os.path.exists(cache_filepath):
        logging.debug(f"[cache] Cache file not found: {cache_filepath}")
        return None

    try:
        with open(cache_filepath, "r") as f:
            return json.load(f)
    except Exception as e:
        logging.error(f"[cache] Exception reading cache: {e}")
        return None


class Cache(plugins.Plugin):
    __author__ = "fmatray"
    __version__ = "1.0.0"
    __license__ = "GPL3"
    __description__ = "A simple plugin to cache AP informations"

    def __init__(self):
        self.options = dict()
        self.ready = False
        self.lock = Lock()
        self.cache_dir = ""
        # Initialize with UTC time
        self.last_clean = datetime.now(timezone.utc)

    def on_loaded(self):
        logging.info("[cache] Plugin loaded.")

    def on_config_changed(self, config):
        try:
            # Safely get configuration values with defaults
            bettercap_conf = config.get("bettercap", {})
            handshake_dir = bettercap_conf.get("handshakes", "/root/handshakes")
            
            self.cache_dir = os.path.join(handshake_dir, "Cache")
            os.makedirs(self.cache_dir, exist_ok=True)
            
            self.last_clean = datetime.now(timezone.utc)
            self.ready = True
            
            logging.info("[cache] Cache plugin configured")
            self.clean_ap_cache()
        except Exception as e:
            logging.error(f"[cache] Configuration error: {e}")
            self.ready = False

    def on_unload(self, ui):
        self.clean_ap_cache()

    def clean_ap_cache(self):
        """
        Removes cache files older than 5 minutes.
        """
        if not self.ready:
            return

        with self.lock:
            current_time = datetime.now(timezone.utc)
            files_to_delete = []
            
            cache_path = pathlib.Path(self.cache_dir)
            if not cache_path.exists():
                return

            # Scan for stale files
            for cache_file in cache_path.glob("*.apCache"):
                try:
                    stat = cache_file.lstat()
                    mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                    
                    # Delete if older than 5 minutes (300 seconds)
                    if (current_time - mtime).total_seconds() > 300:
                        files_to_delete.append(cache_file)
                except FileNotFoundError:
                    continue
                except OSError as e:
                    logging.debug(f"[cache] Error checking file {cache_file}: {e}")

            if files_to_delete:
                logging.info(f"[cache] Cleaning {len(files_to_delete)} stale files")

            # Perform deletion
            for cache_file in files_to_delete:
                try:
                    cache_file.unlink()
                except FileNotFoundError:
                    pass
                except OSError as e:
                    logging.error(f"[cache] Error deleting {cache_file}: {e}")

    def write_ap_cache(self, access_point):
        """
        Writes AP data to a JSON cache file.
        """
        if not self.ready:
            return

        with self.lock:
            try:
                if "mac" not in access_point or "hostname" not in access_point:
                    return

                mac = access_point["mac"].replace(":", "")
                # Sanitize hostname to be safe for filenames
                hostname = re.sub(r"[^a-zA-Z0-9]", "", access_point["hostname"])
                
                # Handle cases where hostname might be empty after sanitization
                if not hostname:
                    hostname = "unknown"

                filename = f"{hostname}_{mac}.apCache"
                file_path = os.path.join(self.cache_dir, filename)

                with open(file_path, "w") as f:
                    json.dump(access_point, f)

            except Exception as e:
                logging.error(f"[cache] Write error for {file_path}: {e}")

    def on_wifi_update(self, agent, access_points):
        if self.ready:
            valid_aps = [
                ap for ap in access_points 
                if ap.get("hostname") and ap["hostname"] not in ["", "<hidden>"]
            ]
            for ap in valid_aps:
                self.write_ap_cache(ap)

    def on_unfiltered_ap_list(self, agent, aps):
        if self.ready:
            valid_aps = [
                ap for ap in aps 
                if ap.get("hostname") and ap["hostname"] not in ["", "<hidden>"]
            ]
            for ap in valid_aps:
                self.write_ap_cache(ap)

    def on_association(self, agent, access_point):
        if self.ready:
            self.write_ap_cache(access_point)

    def on_deauthentication(self, agent, access_point, client_station):
        if self.ready:
            self.write_ap_cache(access_point)

    def on_handshake(self, agent, filename, access_point, client_station):
        if self.ready:
            self.write_ap_cache(access_point)

    def on_ui_update(self, ui):
        if not self.ready:
            return

        current_time = datetime.now(timezone.utc)
        # Check if 60 seconds have passed since last clean
        if (current_time - self.last_clean).total_seconds() > 60:
            self.clean_ap_cache()
            self.last_clean = current_time
