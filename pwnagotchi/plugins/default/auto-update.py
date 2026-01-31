import os
import re
import time
import glob
import shutil
import logging
import platform
import requests
import subprocess
from threading import Lock

import pwnagotchi
import pwnagotchi.plugins as plugins
from pwnagotchi.utils import StatusFile, parse_version as version_to_tuple


def check(version, repo, native=True):
    logging.debug("[auto-update] Checking remote version for %s, local is %s", repo, version)
    info = {
        "repo": repo,
        "current": version,
        "available": None,
        "url": None,
        "native": native,
        "arch": platform.machine(),
    }

    try:
        resp = requests.get(f"https://api.github.com/repos/{repo}/releases/latest")
        resp.raise_for_status()
        latest = resp.json()
        info["available"] = latest_ver = latest["tag_name"].replace("v", "")
        is_arm64 = info["arch"].startswith("aarch")

        local = version_to_tuple(info["current"])
        remote = version_to_tuple(latest_ver)

        if remote > local:
            if not native:
                info["url"] = f"https://github.com/{repo}/archive/{latest['tag_name']}.zip"
            else:
                if is_arm64:
                    # check if this release is compatible with aarch64
                    for asset in latest["assets"]:
                        download_url = asset["browser_download_url"]
                        if download_url.endswith(".zip") and (
                            info["arch"] in download_url or
                            (is_arm64 and "aarch64" in download_url)
                        ):
                            info["url"] = download_url
                            break
    except Exception as e:
        logging.error("[auto-update] Check failed for %s: %s", repo, e)

    return info


def make_path_for(name):
    path = os.path.join("/usr/local/src/", name)
    if os.path.exists(path):
        logging.debug("[auto-update] Deleting %s", path)
        shutil.rmtree(path, ignore_errors=True, onerror=None)
    os.makedirs(path)
    return path


def download_and_unzip(name, path, display, update):
    target = f"{name}_{update['available']}.zip"
    target_path = os.path.join(path, target)

    logging.info("[auto-update] Downloading %s to %s ...", update['url'], target_path)
    display.update(
        force=True,
        new_data={"status": f"Downloading {name} {update['available']} ..."},
    )

    # Use subprocess for better control
    result = subprocess.run(
        ["wget", "-q", update["url"], "-O", target_path],
        check=False
    )

    if result.returncode != 0:
        logging.error("[auto-update] Download failed for %s", update['url'])
        return False

    logging.info("[auto-update] Extracting %s to %s ...", target_path, path)
    display.update(
        force=True,
        new_data={"status": f"Extracting {name} {update['available']} ..."},
    )

    subprocess.run(["unzip", "-o", target_path, "-d", path], check=False)
    return True


def verify(name, path, source_path, display, update):
    display.update(
        force=True,
        new_data={"status": f"Verifying {name} {update['available']} ..."},
    )

    checksums = glob.glob(f"{path}/*.sha256")
    if not checksums:
        if update["native"]:
            logging.warning("[auto-update] Native update without SHA256 checksum file")
            return False

    else:
        checksum = checksums[0]
        logging.info("[auto-update] Verifying %s for %s ...", checksum, source_path)

        try:
            with open(checksum, "rt") as fp:
                expected = fp.read().split("=")[1].strip().lower()

            real = (
                subprocess.getoutput(f'sha256sum "{source_path}"')
                .split(" ")[0]
                .strip()
                .lower()
            )

            if real != expected:
                logging.warning(
                    "%s [auto-update] Checksum mismatch for %s: expected=%s got=%s",
                    source_path, expected, real
                )
                return False
        except Exception as e:
            logging.error("[auto-update] Verification failed: %s", e)
            return False

    return True


def install(display, update):
    name = update["repo"].split("/")[1]
    path = make_path_for(name)

    if not download_and_unzip(name, path, display, update):
        return False

    source_path = os.path.join(path, name)
    if not verify(name, path, source_path, display, update):
        return False

    logging.info("[auto-update] Installing %s ...", name)
    display.update(
        force=True,
        new_data={"status": f"Installing {name} {update['available']} ..."},
    )

    if update["native"]:
        dest_path = subprocess.getoutput(f"which {name}")
        if not dest_path:
            logging.warning("[auto-update] Can't find path for %s", name)
            return False

        logging.info("[auto-update] Stopping %s ...", update['service'])
        os.system(f"service {update['service']} stop")

        # Move new binary
        try:
            shutil.move(source_path, dest_path)
            os.chmod(dest_path, 0o755)
        except Exception as e:
            logging.error("[auto-update] Failed to move binary: %s", e)
            return False

        logging.info("[auto-update] Restarting %s ...", update['service'])
        os.system(f"service {update['service']} start")
    else:
        if not os.path.exists(source_path):
            source_path = f"{source_path}-{update['available']}"

        logging.info("[auto-update] Running pip install ...")
        cmd = f"cd {source_path} && pip3 install . --break-system-packages"
        os.system(cmd)

    return True


def parse_version(cmd):
    try:
        out = subprocess.getoutput(cmd)
        for part in out.split(" "):
            part = part.replace("v", "").strip()
            # Regex to find standard version strings (e.g., 1.2.0 or 1.2.0-rc1)
            if re.search(r"^\d+\.\d+\.\d+.*$", part):
                return part
    except Exception as e:
        logging.debug("[auto-update] Error parsing version for %s: %s", cmd, e)

    logging.warning("[auto-update] Could not parse version from %s, returning 0.0.0", cmd)
    return "0.0.0"


class AutoUpdate(plugins.Plugin):
    __GitHub__ = ""
    __author__ = "evilsocket@gmail.com (edited by: itsdarklikehell)"
    __version__ = "1.1.1"
    __name__ = "auto-update"
    __license__ = "GPL3"
    __description__ = "Checks when updates are available and applies them when internet is available."
    __help__ = "Checks when updates are available and applies them when internet is available."
    __dependencies__ = {
        "apt": ["none"],
        "pip": ["scapy"],
    }
    __defaults__ = {
        "enabled": False,
        "install": False,
        "interval": 1,
    }

    def __init__(self):
        self.ready = False
        self.status = StatusFile("/root/.auto-update")
        self.lock = Lock()
        self.options = dict()

    def on_loaded(self):
        if "interval" not in self.options or not self.options["interval"]:
            logging.error("[auto-update] Interval is not set")
            return
        self.ready = True
        logging.info("[auto-update] Plugin loaded.")

    def on_internet_available(self, agent):
        if self.lock.locked():
            return

        with self.lock:
            logging.debug("[auto-update] Internet connectivity is available (ready %s)", self.ready)

            if not self.ready:
                return

            if self.status.newer_then_hours(self.options["interval"]):
                logging.debug("[auto-update] Last check happened less than %d hours ago", self.options['interval'])
                return

            logging.info("[auto-update] Checking for updates ...")

            display = agent.view()
            prev_status = display.get("status")

            try:
                display.update(
                    force=True, new_data={"status": "Checking for updates ..."}
                )

                to_install = []
                # Define repositories to check
                to_check = [
                    (
                        "jayofelony/bettercap",
                        parse_version("bettercap -version"),
                        True,
                        "bettercap",
                    ),
                    (
                        "jayofelony/pwngrid",
                        parse_version("pwngrid -version"),
                        True,
                        "pwngrid-peer",
                    ),
                    (
                        "jayofelony/pwnagotchi",
                        pwnagotchi.__version__,
                        False,
                        "pwnagotchi",
                    ),
                ]

                for repo, local_version, is_native, svc_name in to_check:
                    info = check(local_version, repo, is_native)
                    if info["url"] is not None:
                        logging.warning(
                            "%s [auto-update] Update for %s available (local version is '%s'): %s",
                            repo, info['current'], info['url']
                        )
                        info["service"] = svc_name
                        to_install.append(info)

                num_updates = len(to_install)
                num_installed = 0

                if num_updates > 0:
                    if self.options["install"]:
                        for update in to_install:
                            plugins.on("updating")
                            if install(display, update):
                                num_installed += 1
                    else:
                        prev_status = f"{num_updates} new update{'s' if num_updates > 1 else ''} available!"

                logging.info("[auto-update] Done")

                self.status.update()

                if num_installed > 0:
                    display.update(force=True, new_data={"status": "Rebooting ..."})
                    time.sleep(3)
                    os.system("service pwnagotchi restart")

            except Exception as e:
                logging.error("[auto-update] %s", e)

            display.update(
                force=True,
                new_data={"status": prev_status if prev_status is not None else ""},
            )
