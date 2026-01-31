import os
import re
import logging
import subprocess
import requests
import platform
import shutil
import glob
from threading import Lock
import time

import pwnagotchi
import pwnagotchi.plugins as plugins
from pwnagotchi.utils import StatusFile, parse_version as version_to_tuple


def check(version, repo, native=True, token=""):
    logging.debug(f"checking remote version for {repo}, local is {version}")
    info = {
        "repo": repo,
        "current": version,
        "available": None,
        "url": None,
        "native": native,
        "arch": platform.machine(),
    }

    headers = {}
    if token != "":
        headers["Authorization"] = f"token {token}"
        resp = requests.get(
            f"https://api.github.com/repos/{repo}/releases/latest", headers=headers
        )
    else:
        resp = requests.get(f"https://api.github.com/repos/{repo}/releases/latest")

    if resp.status_code != 200:
        logging.error(
            f"[Auto-Update] Failed to get latest release for {repo}: {resp.status_code}"
        )
        return info

    remaining_requests = resp.headers.get("X-RateLimit-Remaining")
    logging.debug(f"[Auto-Update] Requests remaining: {remaining_requests}")

    latest = resp.json()
    info["available"] = latest_ver = latest["tag_name"].replace("v", "")
    is_armhf = info["arch"].startswith("arm")
    is_aarch = info["arch"].startswith("aarch")

    local = version_to_tuple(info["current"])
    remote = version_to_tuple(latest_ver)
    if remote > local:
        if not native:
            info["url"] = f"https://github.com/{repo}/archive/{latest['tag_name']}.zip"
        else:
            if is_armhf:
                # check if this release is compatible with armhf
                for asset in latest["assets"]:
                    download_url = asset["browser_download_url"]
                    if download_url.endswith(".zip") and (
                        info["arch"] in download_url
                        or (is_armhf and "armhf" in download_url)
                    ):
                        info["url"] = download_url
                        break
            elif is_aarch:
                # check if this release is compatible with arm64/aarch64
                for asset in latest["assets"]:
                    download_url = asset["browser_download_url"]
                    if download_url.endswith(".zip") and (
                        info["arch"] in download_url
                        or (is_aarch and "aarch" in download_url)
                    ):
                        info["url"] = download_url
                        break

    return info


def make_path_for(name):
    path = os.path.join("/home/pi/", name)
    if os.path.exists(path):
        logging.debug(f"[update] deleting {path}")
        shutil.rmtree(path, ignore_errors=True, onerror=None)
    os.makedirs(path)
    return path


def download_and_unzip(name, path, display, update):
    target = f"{name}_{update['available']}.zip"
    target_path = os.path.join(path, target)

    logging.info(f"[update] downloading {update['url']} to {target_path} ...")
    display.update(
        force=True, new_data={"status": f"Downloading {name} {update['available']} ..."}
    )

    os.system(f"wget -q \"{update['url']}\" -O \"{target_path}\"")

    logging.info(f"[update] extracting {target_path} to {path} ...")
    display.update(
        force=True, new_data={"status": f"Extracting {name} {update['available']} ..."}
    )

    os.system(f'unzip "{target_path}" -d "{path}"')


def verify(name, path, source_path, display, update):
    display.update(
        force=True, new_data={"status": f"Verifying {name} {update['available']} ..."}
    )

    checksums = glob.glob(f"{path}/*.sha256")
    if len(checksums) == 0:
        if update["native"]:
            logging.warning("[update] native update without SHA256 checksum file")
            return False

    else:
        checksum = checksums[0]

        logging.info(f"[update] verifying {checksum} for {source_path} ...")

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
                f"[update] checksum mismatch for {source_path}: expected={expected} got={real}"
            )
            return False

    return True


def install(display, update):

    name = update["repo"].split("/")[1]

    path = make_path_for(name)

    download_and_unzip(name, path, display, update)

    source_path = os.path.join(path, name)
    if not verify(name, path, source_path, display, update):
        return False

    logging.info(f"[update] installing {name} ...")
    display.update(
        force=True, new_data={"status": f"Installing {name} {update['available']} ..."}
    )

    if update["native"]:
        dest_path = subprocess.getoutput(f"which {name}")
        if dest_path == "":
            logging.warning(f"[update] can't find path for {name}")
            return False

        logging.info(f"[update] stopping {update['service']} ...")
        os.system(f"service {update['service']} stop")
        shutil.move(source_path, dest_path)
        os.chmod(f"/usr/local/bin/{name}", 0o755)
        logging.info(f"[update] restarting {update['service']} ...")
        os.system(f"service {update['service']} start")
    else:
        if not os.path.exists(source_path):
            source_path = f"{source_path}-{update['available']}"

        try:
            # Activate the virtual environment and install the package
            subprocess.run(
                [
                    "bash",
                    "-c",
                    f"source /home/pi/.pwn/bin/activate && pip install {source_path}",
                ],
                check=True,
            )

            # Clean up the source directory
            shutil.rmtree(source_path, ignore_errors=True)

        except subprocess.CalledProcessError as e:
            logging.error(f"Installation failed: {e}")
        except Exception as e:
            logging.error(f"Unexpected error: {e}")
    return True


def parse_version(cmd):
    out = subprocess.getoutput(cmd)
    for part in out.split(" "):
        part = part.replace("v", "").strip()
        if re.search(r"^\d+\.\d+\.\d+.*$", part):
            return part
    raise Exception(f'could not parse version from "{cmd}": output=\n{out}')


class AutoUpdate(plugins.Plugin):
    __author__ = "evilsocket@gmail.com"
    __version__ = "1.1.1"
    __name__ = "auto-update"
    __license__ = "GPL3"
    __description__ = "This plugin checks when updates are available and applies them when internet is available."

    def __init__(self):
        self.ready = False
        self.status = StatusFile("/root/.auto-update")
        self.lock = Lock()
        self.options = dict()

    def on_loaded(self):
        if "interval" not in self.options or (
            "interval" in self.options and not self.options["interval"]
        ):
            logging.error("[update] main.plugins.auto-update.interval is not set")
            return
        self.ready = True
        logging.info("[update] plugin loaded.")

    def on_internet_available(self, agent):
        if self.lock.locked():
            return

        with self.lock:
            logging.debug(
                f"[update] internet connectivity is available (ready {self.ready})"
            )

            if not self.ready:
                return

            if self.status.newer_then_hours(self.options["interval"]):
                logging.debug(
                    "[update] last check happened less than %d hours ago"
                    % self.options["interval"]
                )
                return

            logging.info("[update] checking for updates ...")

            display = agent.view()
            prev_status = display.get("status")

            try:
                display.update(
                    force=True, new_data={"status": "Checking for updates ..."}
                )

                to_install = []
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
                    info = check(local_version, repo, is_native, self.options["token"])
                    if info["url"] is not None:

                        logging.warning(
                            "update for %s available (local version is '%s'): %s"
                            % (repo, info["current"], info["url"])
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
                        prev_status = "%d new update%s available!" % (
                            num_updates,
                            "s" if num_updates > 1 else "",
                        )

                logging.info("[update] done")

                self.status.update()

                if num_installed > 0:
                    display.update(force=True, new_data={"status": "Rebooting ..."})
                    time.sleep(3)
                    pwnagotchi.reboot()

            except Exception as e:
                logging.error(f"[update] {e}")

            display.update(
                force=True,
                new_data={"status": prev_status if prev_status is not None else ""},
            )
