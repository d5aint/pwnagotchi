import os
import re
import glob
import time
import logging
from threading import Lock

from flask import make_response, redirect
import pwnagotchi.grid as grid
import pwnagotchi.plugins as plugins
from pwnagotchi.utils import StatusFile, WifiInfo, extract_from_pcap


def parse_pcap(filename):
    logging.info(f"[grid] Parsing {filename} ...")

    net_id = os.path.basename(filename).replace('.pcap', '')

    if '_' in net_id:
        # /root/handshakes/ESSID_BSSID.pcap
        essid, bssid = net_id.split('_')
    else:
        # /root/handshakes/BSSID.pcap
        essid, bssid = '', net_id

    mac_re = re.compile(r'[0-9a-fA-F]{12}')
    if not mac_re.match(bssid):
        return '', ''

    # Format BSSID from aabbccddeeff to aa:bb:cc:dd:ee:ff
    it = iter(bssid)
    bssid = ':'.join([a + b for a, b in zip(it, it)])

    info = {
        WifiInfo.ESSID: essid,
        WifiInfo.BSSID: bssid,
    }

    try:
        # Attempt to extract precise info from the PCAP file itself
        info = extract_from_pcap(filename, [WifiInfo.BSSID, WifiInfo.ESSID])
    except Exception as e:
        logging.error(f"[grid] {e}")

    return info[WifiInfo.ESSID], info[WifiInfo.BSSID]


class Grid(plugins.Plugin):
    __author__ = 'evilsocket@gmail.com'
    __version__ = "1.1.0"
    __license__ = 'GPL3'
    __description__ = 'This plugin signals the unit cryptographic identity and list of pwned networks to opwngrid.xyz'
    __defaults__ = {
        'report': True,
        'exclude': [],
    }

    def __init__(self):
        self.options = dict()
        self.report = StatusFile('/root/.api-report.json', data_format='json')
        self.unread_messages = 0
        self.total_messages = 0
        self.lock = Lock()

    def is_excluded(self, what, agent):
        """
        Checks if the SSID or BSSID is in the main configuration whitelist.
        """
        config = agent.config()
        if 'whitelist' not in config['main']:
            return False

        what = what.lower()
        for skip in config['main']['whitelist']:
            skip = skip.lower()
            if skip in what or skip.replace(':', '') in what:
                return True
        return False

    def on_loaded(self):
        logging.info("[grid] Plugin loaded.")

    def on_webhook(self, path, request):
        response = make_response(redirect("https://opwngrid.xyz", code=302))
        return response

    def set_reported(self, reported, net_id):
        if net_id not in reported:
            reported.append(net_id)
        self.report.update(data={'reported': reported})

    def check_inbox(self, agent):
        logging.debug("[grid] Checking mailbox ...")
        messages = grid.inbox()
        self.total_messages = len(messages)
        self.unread_messages = len([m for m in messages if m['seen_at'] is None])

        if self.unread_messages:
            plugins.on('unread_inbox', self.unread_messages)
            logging.debug(f"[grid] Unread:{self.unread_messages} total:{self.total_messages}")
            agent.view().on_unread_messages(self.unread_messages, self.total_messages)

    def check_handshakes(self, agent):
        logging.debug("[grid] Checking pcap's")
        config = agent.config()

        handshake_dir = config['bettercap']['handshakes']
        pcap_files = glob.glob(os.path.join(handshake_dir, "*.pcap"))

        num_networks = len(pcap_files)
        reported = self.report.data_field_or('reported', default=[])
        num_reported = len(reported)
        num_new = num_networks - num_reported

        if num_new > 0:
            if self.options.get('report', True):
                logging.info(f"[grid] {num_new} new networks to report")
                logging.debug(f"[grid] self.options: {self.options}")
                logging.debug(f" Exclude: {config['main'].get('whitelist', [])}")

                for pcap_file in pcap_files:
                    net_id = os.path.basename(pcap_file).replace('.pcap', '')

                    if net_id not in reported:
                        if self.is_excluded(net_id, agent):
                            logging.debug(f"[grid] Skipping {pcap_file} due to exclusion filter")
                            self.set_reported(reported, net_id)
                            continue

                        essid, bssid = parse_pcap(pcap_file)
                        if bssid:
                            if self.is_excluded(essid, agent) or self.is_excluded(bssid, agent):
                                logging.debug(f"[grid] Not reporting {pcap_file} due to exclusion filter")
                                self.set_reported(reported, net_id)
                            else:
                                if grid.report_ap(essid, bssid):
                                    self.set_reported(reported, net_id)
                                time.sleep(1.5)
                        else:
                            logging.warning("[grid] No bssid found?!")
            else:
                logging.debug("[grid] Reporting disabled")

    def on_internet_available(self, agent):
        logging.debug("[grid] Internet available")

        if self.lock.locked():
            return

        with self.lock:
            try:
                grid.update_data(agent.last_session)
            except Exception as e:
                logging.error(f"[grid] Error connecting to the pwngrid-peer service: {e}")
                logging.debug(e, exc_info=True)
                return

            try:
                self.check_inbox(agent)
            except Exception as e:
                logging.error(f"[grid] Error while checking inbox: {e}")
                logging.debug(e, exc_info=True)

            try:
                self.check_handshakes(agent)
            except Exception as e:
                logging.error(f"[grid] Error while checking pcaps: {e}")
                logging.debug(e, exc_info=True)