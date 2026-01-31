import os
import glob
import time
import socket
import logging
import threading
import subprocess
import pwnagotchi.plugins as plugins
from pwnagotchi.utils import StatusFile

class AutoBackup(plugins.Plugin):
    __author__ = 'evilsocket@gmail.com (edited by WPA2, itsdarklikehell, d5aint)'
    __version__ = "1.1.3"
    __license__ = 'GPL3'
    __description__ = 'Backs up files when internet is available. Supports exclusions, rotation, and non-blocking threads.'
    __name__ = 'AutoBackup'
    __help__ = 'Backs up files when internet is available.'
    __dependencies__ = {
        "apt": ["tar"],
    }
    __defaults__ = {
        "enabled": False,
        "interval": "daily",  # Can be "daily", "hourly", or integer (minutes)
        "max_tries": 0,
        "max_backups_to_keep": 10,
        "backup_location": "/root/",
        "exclude": [],
        "files": [
            "/root/brain.nn",
            "/root/brain.json",
            "/root/.api-report.json",
            "/root/peers/",
            "/etc/pwnagotchi/"
        ],
        "commands": ["tar czf {backup_file} {exclude} {files}"],
    }

    def __init__(self):
        self.ready = False
        self.tries = 0
        # Used to throttle repeated log messages for "backup not due yet"
        self.last_not_due_logged = 0
        self.status_file_path = '/root/.auto-backup'
        self.status = StatusFile(self.status_file_path)
        self.lock = threading.Lock()
        self.backup_in_progress = False

    def on_loaded(self):
        # Validate required options
        required_opts = ['files', 'interval', 'backup_location', 'max_tries']
        for opt in required_opts:
            if opt not in self.options or self.options[opt] is None:
                logging.error(f"[auto-backup] Option '{opt}' is not set.")
                return

        # Create backup directory if it doesn't exist
        if not os.path.exists(self.options['backup_location']):
            try:
                os.makedirs(self.options['backup_location'])
                logging.info(f"[auto-backup] Created backup directory: {self.options['backup_location']}")
            except OSError as e:
                logging.error(f"[auto-backup] Failed to create backup directory: {e}")
                return

        self.ready = True
        logging.info("[auto-backup] Plugin loaded.")

    def get_interval_seconds(self):
        """
        Convert the interval option into seconds.
        Supports:
          - "daily" for 24 hours,
          - "hourly" for 60 minutes,
          - or a numeric value (interpreted as minutes).
        """
        interval = self.options['interval']
        if isinstance(interval, str):
            if interval.lower() == "daily":
                return 24 * 60 * 60
            elif interval.lower() == "hourly":
                return 60 * 60
            else:
                try:
                    minutes = float(interval)
                    return minutes * 60
                except ValueError:
                    logging.error("[auto-backup] Invalid interval format. Defaulting to daily.")
                    return 24 * 60 * 60
        elif isinstance(interval, (int, float)):
            return float(interval) * 60
        else:
            logging.error("[auto-backup] Unrecognized interval type. Defaulting to daily.")
            return 24 * 60 * 60

    def is_backup_due(self):
        """
        Determines if enough time has passed since the last backup.
        If the status file does not exist, a backup is due.
        """
        interval_sec = self.get_interval_seconds()
        try:
            # Check the actual modification time of the status file
            last_backup = os.path.getmtime(self.status_file_path)
        except OSError:
            # Status file doesn't exist -> backup is due
            return True

        now = time.time()
        return (now - last_backup) >= interval_sec

    def _cleanup_old_backups(self):
        """Deletes the oldest backups if we exceed the limit."""
        try:
            backup_dir = self.options['backup_location']
            max_keep = self.options.get('max_backups_to_keep', 10)
            hostname = socket.gethostname()

            if max_keep <= 0:
                return

            # Filter by this device's hostname to avoid deleting other backups
            search_pattern = os.path.join(backup_dir, f"{hostname}-backup-*.tar.gz")
            files = glob.glob(search_pattern)

            if not files:
                return

            # Sort files by modification time (oldest first)
            files.sort(key=os.path.getmtime)

            # Calculate how many to delete
            if len(files) > max_keep:
                num_to_delete = len(files) - max_keep
                logging.info(f"[auto-backup] Cleaning up {num_to_delete} old backup(s)...")

                for old_file in files[:num_to_delete]:
                    try:
                        os.remove(old_file)
                        logging.debug(f"[auto-backup] Deleted: {os.path.basename(old_file)}")
                    except OSError as e:
                        logging.error(f"[auto-backup] Failed to delete {old_file}: {e}")

        except Exception as e:
            logging.error(f"[auto-backup] Cleanup error: {e}")

    def _run_backup_thread(self, agent, existing_files):
        """
        Executes the backup process in a separate thread to avoid blocking the UI.
        """
        with self.lock:
            try:
                display = agent.view()
                logging.info("[auto-backup] Starting backup...")

                # Update UI (safe within thread for display.set/update)
                try:
                    display.set('status', 'Backing up...')
                    display.update(force=True)
                except Exception:
                    pass

                # Prepare variables for the command
                files_to_backup = " ".join(f'"{f}"' for f in existing_files)

                excludes = ""
                exclude_list = self.options.get('exclude', [])
                if exclude_list:
                    for pattern in exclude_list:
                        excludes += f" --exclude='{pattern}'"

                hostname = socket.gethostname()
                timestamp = time.strftime("%Y%m%d-%H%M%S")
                filename = f"{hostname}-backup-{timestamp}.tar.gz"
                backup_file = os.path.join(self.options['backup_location'], filename)

                # Run Commands
                for cmd in self.options['commands']:
                    formatted_cmd = cmd.format(
                        backup_file=backup_file,
                        files=files_to_backup,
                        exclude=excludes
                    )

                    logging.debug(f"[auto-backup] Executing: {formatted_cmd}")

                    process = subprocess.Popen(
                        formatted_cmd,
                        shell=True,
                        stdin=None,
                        stdout=open("/dev/null", "w"),
                        stderr=subprocess.PIPE,
                        executable="/bin/bash"
                    )
                    _, stderr = process.communicate()

                    if process.returncode > 0:
                        raise OSError(f"Command failed (rc: {process.returncode}): {stderr.decode('utf-8')}")

                logging.info(f"[auto-backup] Backup successful: {backup_file}")

                try:
                    display.set('status', 'Backup done!')
                    display.update(force=True)
                except Exception:
                    pass

                self._cleanup_old_backups()
                self.tries = 0
                self.status.update()

            except Exception as e:
                self.tries += 1
                logging.error(f"[auto-backup] Error: {e}")
                try:
                    display.set('status', 'Backup failed!')
                    display.update(force=True)
                except Exception:
                    pass
            finally:
                self.backup_in_progress = False

    def on_internet_available(self, agent):
        if not self.ready:
            return

        # Check retry limit
        if self.options['max_tries'] > 0 and self.tries >= self.options['max_tries']:
            logging.info("[auto-backup] Maximum tries reached, skipping backup.")
            return

        # Check if backup is due
        if not self.is_backup_due():
            now = time.time()
            # Throttle log spam to once every 10 minutes
            if now - self.last_not_due_logged > 600:
                logging.debug(f"[auto-backup] Backup not due yet. Interval: {self.options['interval']}")
                self.last_not_due_logged = now
            return

        # Filter out files that don't exist
        existing_files = list(filter(lambda f: os.path.exists(f), self.options['files']))
        if not existing_files:
            logging.warning("[auto-backup] No valid files found to backup.")
            return

        # Concurrency Check
        if self.backup_in_progress:
            return

        self.backup_in_progress = True

        # Start the backup thread
        threading.Thread(
            target=self._run_backup_thread,
            args=(agent, existing_files),
            daemon=True,
            name="AutoBackupThread"
        ).start()

    def on_webhook(self, path, request):
        logging.info(f"[auto-backup] Webhook triggered: {path}")
        # This allows triggering a backup manually via web request if needed

    def on_unload(self, ui):
        logging.info("[auto-backup] Plugin unloaded.")
