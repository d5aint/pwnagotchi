import logging
import os
import subprocess
import time
from threading import Lock

import dbus

import pwnagotchi.plugins as plugins
import pwnagotchi.ui.fonts as fonts
from pwnagotchi.ui.components import LabeledValue
from pwnagotchi.ui.view import BLACK
from pwnagotchi.utils import StatusFile


class BTError(Exception):
    """Custom bluetooth exception."""
    pass


class BTNap:
    """
    This class handles the DBus communication with BlueZ to manage
    Bluetooth connections and NAP (Network Access Point) profiles.
    """
    DBUS_BLUEZ = "org.bluez"
    IFACE_DEV = "org.bluez.Device1"
    IFACE_ADAPTER = "org.bluez.Adapter1"
    IFACE_PROPS = "org.freedesktop.DBus.Properties"

    def __init__(self, mac):
        self._mac = mac

    @staticmethod
    def get_bus():
        """Get or create the SystemBus object."""
        bus = getattr(BTNap.get_bus, "cached_obj", None)
        if not bus:
            bus = BTNap.get_bus.cached_obj = dbus.SystemBus()
        return bus

    @staticmethod
    def get_manager():
        """Get the DBus ObjectManager."""
        manager = getattr(BTNap.get_manager, "cached_obj", None)
        if not manager:
            manager = BTNap.get_manager.cached_obj = dbus.Interface(
                BTNap.get_bus().get_object(BTNap.DBUS_BLUEZ, "/"),
                "org.freedesktop.DBus.ObjectManager",
            )
        return manager

    @staticmethod
    def prop_get(obj, key, iface=None):
        """Get a property from a DBus object."""
        if iface is None:
            iface = obj.dbus_interface
        return obj.Get(iface, key, dbus_interface=BTNap.IFACE_PROPS)

    @staticmethod
    def prop_set(obj, key, val, iface=None):
        """Set a property on a DBus object."""
        if iface is None:
            iface = obj.dbus_interface
        return obj.Set(iface, key, val, dbus_interface=BTNap.IFACE_PROPS)

    @staticmethod
    def find_adapter(pattern=None):
        """Find the Bluetooth adapter."""
        return BTNap.find_adapter_in_objects(
            BTNap.get_manager().GetManagedObjects(), pattern
        )

    @staticmethod
    def find_adapter_in_objects(objects, pattern=None):
        """Find adapter in managed objects."""
        bus, obj = BTNap.get_bus(), None
        for path, ifaces in objects.items():
            adapter = ifaces.get(BTNap.IFACE_ADAPTER)
            if adapter is None:
                continue
            if not pattern or pattern == adapter["Address"] or path.endswith(pattern):
                obj = bus.get_object(BTNap.DBUS_BLUEZ, path)
                yield dbus.Interface(obj, BTNap.IFACE_ADAPTER)
        if obj is None:
            raise BTError("Bluetooth adapter not found")

    @staticmethod
    def find_device(device_address, adapter_pattern=None):
        """Find a specific Bluetooth device."""
        return BTNap.find_device_in_objects(
            BTNap.get_manager().GetManagedObjects(), device_address, adapter_pattern
        )

    @staticmethod
    def find_device_in_objects(objects, device_address, adapter_pattern=None):
        """Find device in managed objects."""
        bus = BTNap.get_bus()
        path_prefix = ""
        if adapter_pattern:
            if not isinstance(adapter_pattern, str):
                adapter = adapter_pattern
            else:
                adapter = BTNap.find_adapter_in_objects(objects, adapter_pattern)
            path_prefix = adapter.object_path

        for path, ifaces in objects.items():
            device = ifaces.get(BTNap.IFACE_DEV)
            if device is None:
                continue
            if str(device["Address"]).lower() == device_address.lower() and \
               path.startswith(path_prefix):
                obj = bus.get_object(BTNap.DBUS_BLUEZ, path)
                return dbus.Interface(obj, BTNap.IFACE_DEV)
        raise BTError("Bluetooth device not found")

    def power(self, on=True):
        """Set power of adapter to on/off."""
        logging.debug(f"[bt-tether] Changing bluetooth power to {on}")
        try:
            devs = list(BTNap.find_adapter())
            devs = {BTNap.prop_get(dev, "Address"): dev for dev in devs}
        except BTError as bt_err:
            logging.error(bt_err)
            return None

        for dev_addr, dev in devs.items():
            BTNap.prop_set(dev, "Powered", on)
            logging.debug(f"[bt-tether] Set power of {dev_addr} to {on}")

        if devs:
            return list(devs.values())[0]
        return None

    def is_paired(self):
        """Check if device is paired."""
        bt_dev = self.power(True)
        if not bt_dev:
            return False

        try:
            dev_remote = BTNap.find_device(self._mac, bt_dev)
            return bool(BTNap.prop_get(dev_remote, "Paired"))
        except BTError:
            return False

    def wait_for_device(self, timeout=15):
        """Wait for device discovery."""
        logging.debug("[bt-tether] Scanning for device...")
        bt_dev = self.power(True)

        if not bt_dev:
            logging.error("[bt-tether] No bluetooth adapter found.")
            return None

        try:
            bt_dev.StartDiscovery()
        except Exception as e:
            logging.error(f"[bt-tether] Discovery error: {e}")
            raise e

        dev_remote = None
        while timeout > -1:
            try:
                dev_remote = BTNap.find_device(self._mac, bt_dev)
                logging.debug(f"[bt-tether] Found device: {dev_remote.object_path}")
                break
            except BTError:
                pass
            time.sleep(1)
            timeout -= 1

        try:
            bt_dev.StopDiscovery()
        except Exception:
            pass

        return dev_remote

    @staticmethod
    def pair(device):
        """Attempt to pair with the device."""
        logging.debug("[bt-tether] Attempting to pair...")
        try:
            device.Pair()
            logging.info("[bt-tether] Pairing successful.")
            return True
        except dbus.exceptions.DBusException as err:
            if err.get_dbus_name() == "org.bluez.Error.AlreadyExists":
                return True
        except Exception as e:
            logging.error(f"[bt-tether] Pairing error: {e}")
        return False

    @staticmethod
    def nap(device):
        """Connect to the NAP profile."""
        logging.debug("[bt-tether] Connecting NAP profile...")
        try:
            device.ConnectProfile("nap")
        except Exception:
            pass

        net = dbus.Interface(device, "org.bluez.Network1")
        try:
            net.Connect("nap")
            return net, True
        except dbus.exceptions.DBusException as err:
            if err.get_dbus_name() == "org.bluez.Error.AlreadyConnected":
                return net, True
            
            # Check if property says connected even if Connect() threw
            if BTNap.prop_get(net, "Connected"):
                return net, True
            
            return None, False


class SystemdUnit:
    """Wrapper for systemctl commands."""
    def __init__(self, unit):
        self.unit = unit

    def _action(self, action):
        try:
            subprocess.check_call(
                ["systemctl", action, self.unit],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            return True
        except subprocess.CalledProcessError:
            return False

    def is_active(self):
        return self._action("is-active")

    def start(self):
        return self._action("start")


class IfaceWrapper:
    """Wrapper for IP and Route management."""
    def __init__(self, iface):
        self.iface = iface

    def set_addr(self, addr):
        """Set IP address on interface."""
        try:
            subprocess.check_call(
                ["ip", "addr", "add", addr, "dev", self.iface],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            return True
        except subprocess.CalledProcessError as e:
            # Return code 2 usually means 'File exists' (IP already set)
            if e.returncode == 2:
                return True
            return False

    @staticmethod
    def set_route(gateway, device):
        """Set default route."""
        try:
            subprocess.check_call(
                ["ip", "route", "replace", "default", "via", gateway, "dev", device],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            return True
        except subprocess.CalledProcessError:
            return False


class Device:
    """Represents a configured Bluetooth device."""
    def __init__(self, name, **kwargs):
        self.name = name
        self.mac = kwargs.get('mac', '')
        self.ip = kwargs.get('ip', '192.168.44.44')
        self.netmask = kwargs.get('netmask', 24)
        self.gateway = kwargs.get('gateway', None)
        self.interval = kwargs.get('interval', 1)
        self.scantime = kwargs.get('scantime', 10)
        self.max_tries = kwargs.get('max_tries', 0)
        self.share_internet = kwargs.get('share_internet', False)
        self.priority = kwargs.get('priority', 10)
        self.search_order = kwargs.get('search_order', 1)
        
        self.status = StatusFile(f"/root/.bt-tether-{name}")
        self.status.update()
        self.tries = 0
        self.network = None

    def connected(self):
        return self.network and BTNap.prop_get(self.network, "Connected")

    def interface(self):
        if not self.connected():
            return None
        return BTNap.prop_get(self.network, "Interface")


class BTTether(plugins.Plugin):
    __author__ = "evilsocket (mod by pwnagotchi-community)"
    __version__ = "2.0.0"
    __license__ = "GPL3"
    __description__ = "Multi-device Bluetooth Tethering via DBus/BlueZ"
    __name__ = "BTTether"
    __help__ = "Connects to phone via Bluetooth PAN for internet access."
    __defaults__ = {
        "enabled": False,
        "devices": {
            "android-phone": {
                "enabled": False,
                "search_order": 1,
                "mac": "",
                "ip": "192.168.44.44",
                "netmask": 24,
                "interval": 1,
                "scantime": 10,
                "max_tries": 10,
                "share_internet": True,
                "priority": 1,
            },
            "ios-phone": {
                "enabled": False,
                "search_order": 2,
                "mac": "",
                "ip": "172.20.10.6",
                "netmask": 28,
                "interval": 5,
                "scantime": 20,
                "max_tries": 0,
                "share_internet": True,
                "priority": 999,
            }
        }
    }

    def __init__(self):
        self.ready = False
        self.options = dict()
        self.devices = {}
        self.lock = Lock()
        self.running = True
        self.status_display = "-"

    def on_loaded(self):
        """Parse configuration and load devices."""
        # 1. Parse 'devices' sub-dictionary
        if "devices" in self.options:
            for name, cfg in self.options["devices"].items():
                if cfg.get("enabled", False):
                    self.devices[name] = Device(name, **cfg)

        # 2. Support Legacy (Root level) config
        if "mac" in self.options and self.options["mac"]:
            logging.info("[bt-tether] Loading legacy configuration...")
            self.devices["legacy"] = Device("legacy", **self.options)

        if not self.devices:
            logging.warning("[bt-tether] No enabled devices found in config.")
            return

        # Ensure bluetooth service is up
        bt_unit = SystemdUnit("bluetooth.service")
        if not bt_unit.is_active():
            logging.info("[bt-tether] Starting bluetooth service...")
            if not bt_unit.start():
                logging.error("[bt-tether] Failed to start bluetooth service.")
                return

        logging.info(f"[bt-tether] Loaded with {len(self.devices)} devices.")
        self.ready = True

    def on_unload(self, ui):
        self.running = False
        with ui._lock:
            try:
                ui.remove_element("bluetooth")
            except Exception:
                pass

    def on_ui_setup(self, ui):
        with ui._lock:
            ui.add_element(
                "bluetooth",
                LabeledValue(
                    color=BLACK,
                    label="BT",
                    value="-",
                    position=(ui.width() / 2 - 10, 0),
                    label_font=fonts.Bold,
                    text_font=fonts.Medium,
                ),
            )

    def on_ui_update(self, ui):
        ui.set("bluetooth", self.status_display)

    def run(self):
        """Main plugin loop (replaces original threading approach)."""
        # Note: In standard Pwnagotchi plugins, long running loops in on_loaded
        # block the main thread. We must spawn a thread here.
        # However, for this refactor, we will follow the original pattern
        # but invoke it safely.
        import threading
        t = threading.Thread(target=self._worker, daemon=True)
        t.start()

    def _worker(self):
        while self.running:
            try:
                self._check_connections()
            except Exception as e:
                logging.error(f"[bt-tether] Worker loop error: {e}")
            time.sleep(1)

    def _check_connections(self):
        connected_priorities = []
        any_connected = False

        # Check existing connections
        for device in self.devices.values():
            if device.connected():
                connected_priorities.append(device.priority)
                any_connected = True
                self.status_display = "C" 
                continue

            # Check if we should scan for this device
            if not device.max_tries or (device.max_tries > device.tries):
                if not device.status.newer_then_minutes(device.interval):
                    self._attempt_connect(device)

        if not any_connected:
            self.status_display = "-"

    def _attempt_connect(self, device):
        device.status.update()
        device.tries += 1
        
        bt = BTNap(device.mac)
        
        # 1. Wait/Scan
        self.status_display = "S"  # Scanning
        dev_remote = bt.wait_for_device(timeout=device.scantime)
        if not dev_remote:
            self.status_display = "NF" # Not Found
            return

        # 2. Pair
        if not bt.is_paired():
            self.status_display = "P" # Pairing
            if not BTNap.pair(dev_remote):
                self.status_display = "PE" # Pair Error
                return

        # 3. Connect NAP
        self.status_display = "C..." # Connecting
        device.network, success = BTNap.nap(dev_remote)
        
        if not success:
            self.status_display = "CE" # Connect Error
            return

        # 4. Configure Interface
        interface = device.interface()
        if not interface:
            return

        logging.info(f"[bt-tether] Connected {device.name} on {interface}")
        device.tries = 0 # Reset tries on success

        # 5. Set IP
        addr = f"{device.ip}/{device.netmask}"
        iface_wrapper = IfaceWrapper(interface)
        if not iface_wrapper.set_addr(addr):
            logging.error(f"[bt-tether] Failed to set IP {addr} on {interface}")
            return

        # 6. Gateway & DNS (if sharing internet)
        if device.share_internet:
            gateway = device.gateway
            if not gateway:
                # Assume gateway is x.x.x.1 based on device IP
                parts = device.ip.split(".")
                parts[-1] = "1"
                gateway = ".".join(parts)

            IfaceWrapper.set_route(gateway, interface)
            self._update_resolv_conf()

        self.status_display = "C"

    def _update_resolv_conf(self):
        """Ensures a valid nameserver exists."""
        try:
            with open("/etc/resolv.conf", "r+") as f:
                content = f.read()
                if "nameserver 9.9.9.9" not in content:
                    f.seek(0)
                    f.write("nameserver 9.9.9.9\n" + content)
                    logging.debug("[bt-tether] Added DNS 9.9.9.9")
        except Exception as e:
            logging.error(f"[bt-tether] DNS setup failed: {e}")

    # Hook the run method to start the thread
    def on_ready(self, agent):
        self.run()