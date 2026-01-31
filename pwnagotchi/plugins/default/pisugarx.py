import time
import logging
import threading
import smbus
from collections import deque
from flask import render_template_string

import pwnagotchi
import pwnagotchi.plugins as plugins
import pwnagotchi.ui.fonts as fonts
from pwnagotchi.ui.components import LabeledValue
from pwnagotchi.ui.view import BLACK

# --- Constants & Configuration ---
PISUGAR_ADDRESSES = {
    "PiSugar2": 0x75,       # PiSugar2 / 2 Plus
    "PiSugar3": 0x57,       # PiSugar3 / 3 Plus
    "PiSugar2 RTC": 0x32,   # PiSugar2 RTC
    "PiSugar3 RTC": 0x68    # PiSugar3 RTC
}

# Battery Curves for Voltage -> % estimation (PiSugar 2 legacy)
CURVE_5312 = [
    (4.10, 100.0), (4.05, 95.0), (3.90, 88.0), (3.80, 77.0),
    (3.70, 65.0),  (3.62, 55.0), (3.58, 49.0), (3.49, 25.6),
    (3.32, 4.5),   (3.10, 0.0),
]

CURVE_5209 = [
    (4.16, 100.0), (4.05, 95.0), (4.00, 80.0), (3.92, 65.0),
    (3.86, 40.0),  (3.79, 25.5), (3.66, 10.0), (3.52, 6.5),
    (3.49, 3.2),   (3.10, 0.0),
]


class PiSugarServer:
    """
    Handles background communication with PiSugar hardware via I2C.
    Supports PiSugar 2, 2 Plus, 3, and 3 Plus.
    """
    def __init__(self):
        try:
            self._bus = smbus.SMBus(1)
        except Exception as e:
            logging.error(f"[psugar] Failed to initialize SMBus: {e}")
            self._bus = None

        self.ready = False
        self.model = None
        self.address = 0
        self.i2creg = []

        # Metrics
        self.battery_voltage = 0.00
        self.battery_level = 0
        self.temperature = 0
        self.power_plugged = False
        self.allow_charging = True

        # Configuration / State
        self.voltage_history = deque(maxlen=10)
        self.lowpower_shutdown = True
        self.lowpower_shutdown_level = 5
        self.max_charge_voltage_protection = False
        self.max_protection_level = 80

        # Start hardware connection thread
        if self._bus:
            self.connection_thread = threading.Thread(target=self._connect_device, daemon=True)
            self.connection_thread.start()

    def _check_device(self, address, reg=0):
        try:
            return self._bus.read_byte_data(address, reg)
        except OSError:
            return None

    def _connect_device(self):
        """Identifies the PiSugar model and initializes registers."""
        while self.model is None:
            # Check for PiSugar 2 / 2 Plus
            if self._check_device(PISUGAR_ADDRESSES["PiSugar2"]) is not None:
                self.address = PISUGAR_ADDRESSES["PiSugar2"]
                # Distinguish between 2 and 2 Plus using register 0xC2
                if self._check_device(self.address, 0xC2) != 0:
                    self.model = "PiSugar2Plus"
                else:
                    self.model = "PiSugar2"
                self._device_init_legacy()

            # Check for PiSugar 3 / 3 Plus
            elif self._check_device(PISUGAR_ADDRESSES["PiSugar3"]) is not None:
                self.model = 'PiSugar3'
                self.address = PISUGAR_ADDRESSES["PiSugar3"]
                # PiSugar 3 generally doesn't need complex init for basic reads

            else:
                logging.debug("[psugar] Device not found. Retrying in 5s...")
                time.sleep(5)
                continue

        logging.info(f"[psugar] Connected: {self.model}")

        # Start polling thread
        self._start_timer()

        # Wait for first data dump
        while len(self.i2creg) < 256:
            time.sleep(1)

        self.ready = True
        logging.info(f"[psugar] {self.model} Ready.")

    def _device_init_legacy(self):
        """GPIO and Boost initialization for PiSugar 2 variants."""
        try:
            if self.model == "PiSugar2Plus":
                self._write_bits(0x52, 0b00000010, mode='or')
                self._write_bits(0x54, 0b00000010, mode='or')
                self._write_bits(0x52, 0b00000100, mode='or')
                self._write_bits(0x29, 0b10111111, mode='and')
                # Complex register ops simplified for brevity
                val_52 = self._bus.read_byte_data(self.address, 0x52)
                self._bus.write_byte_data(self.address, 0x52, (val_52 & 0b10011111) | 0b01000000)
                self._write_bits(0xc2, 0b00010000, mode='or')
                # Init boost: 0x3f * 50ma = 3A
                val_30 = self._bus.read_byte_data(self.address, 0x30)
                self._bus.write_byte_data(self.address, 0x30, (val_30 & 0b11000000) | 0x3f)

            elif self.model == "PiSugar2":
                # GPIO Init
                current = self._bus.read_byte_data(self.address, 0x51)
                self._bus.write_byte_data(self.address, 0x51, (current & 0b11110011) | 0b00000100)
                self._write_bits(0x53, 0b00000010, mode='or')
        except Exception as e:
            logging.error(f"[psugar] Init Error: {e}")

    def _write_bits(self, reg, val, mode='or'):
        try:
            current = self._bus.read_byte_data(self.address, reg)
            if mode == 'or':
                self._bus.write_byte_data(self.address, reg, current | val)
            elif mode == 'and':
                self._bus.write_byte_data(self.address, reg, current & val)
        except OSError:
            pass

    def _start_timer(self):
        thread = threading.Thread(target=self._update_loop, daemon=True)
        thread.start()

    def _update_loop(self):
        """Main polling loop."""
        while True:
            try:
                # For PiSugar 2/2+, temporarily disable charging to get accurate voltage reading
                if self.model in ['PiSugar2', 'PiSugar2Plus']:
                    self._set_charging(False)
                    time.sleep(0.05)

                # Dump all 256 registers
                new_reg_data = []
                for i in range(0, 256, 32):
                    chunk = self._bus.read_i2c_block_data(self.address, i, 32)
                    new_reg_data.extend(chunk)
                    time.sleep(0.02)  # Slight delay to be nice to the bus

                self.i2creg = new_reg_data

                # Process Data based on Model
                if self.model == 'PiSugar3':
                    self._process_pisugar3()
                elif self.model == 'PiSugar2':
                    self._process_pisugar2()
                    self._set_charging(True)
                elif self.model == 'PiSugar2Plus':
                    self._process_pisugar2plus()
                    self._set_charging(True)

                # Low Power Shutdown Check
                if (self.lowpower_shutdown and
                        self.battery_level < self.lowpower_shutdown_level and
                        not self.power_plugged):
                    logging.warning(
                        f"[psugar] Battery critical ({self.battery_level}%). Shutting down..."
                    )
                    self.shutdown()
                    pwnagotchi.shutdown()

            except Exception as e:
                logging.debug(f"[psugar] Read Error: {e}")

            time.sleep(3)

    def _process_pisugar3(self):
        # Voltage: High byte 0x22, Low byte 0x23
        low = self.i2creg[0x23]
        high = self.i2creg[0x22]
        self.battery_voltage = ((high << 8) + low) / 1000.0

        # Percentage: Register 0x2A (Dedicated fuel gauge)
        self.battery_level = self.i2creg[0x2a]

        # Temp: Register 0x04 (Offset -40)
        self.temperature = self.i2creg[0x04] - 40

        # Status: Register 0x02
        ctr1 = self.i2creg[0x02]
        self.power_plugged = (ctr1 & (1 << 7)) != 0
        self.allow_charging = (ctr1 & (1 << 6)) != 0

        # Max Charge Protection Logic
        if self.max_charge_voltage_protection:
            current_conf = self._bus.read_byte_data(self.address, 0x20)
            if self.battery_level > self.max_protection_level:
                # Disable charging bit
                self._write_protected(0x20, current_conf | 0b10000000)
            else:
                self._write_protected(0x20, current_conf & 0b01111111)

    def _process_pisugar2(self):
        high = self.i2creg[0xa3]
        low = self.i2creg[0xa2]
        if high & 0x20:
            self.battery_voltage = (2600.0 - (((high | 0b11000000) << 8) + low) * 0.26855) / 1000.0
        else:
            self.battery_voltage = (2600.0 + (((high & 0x1f) << 8) + low) * 0.26855) / 1000.0

        self.power_plugged = (self.i2creg[0x55] & 0b00010000) != 0
        self.voltage_history.append(self.battery_voltage)
        self.battery_level = self._convert_voltage_to_level(CURVE_5209)

    def _process_pisugar2plus(self):
        low = self.i2creg[0xd0]
        high = self.i2creg[0xd1]
        self.battery_voltage = (
            (((high & 0b00111111) << 8) + low) * 0.26855 + 2600.0
        ) / 1000.0
        self.power_plugged = (self.i2creg[0xdd] == 0x1f)
        self.voltage_history.append(self.battery_voltage)
        self.battery_level = self._convert_voltage_to_level(CURVE_5312)

    def _write_protected(self, reg, data):
        """Writes to a protected register on PiSugar 3."""
        self._bus.write_byte_data(self.address, 0x0B, 0x29)  # Unlock
        self._bus.write_byte_data(self.address, reg, data)
        self._bus.write_byte_data(self.address, 0x0B, 0x00)  # Lock

    def _set_charging(self, enable):
        """Enables/Disables charging for legacy models."""
        try:
            if self.model == 'PiSugar2':
                # Toggle GPIO2 output and charging bit
                mask_54 = 0b00000100 if enable else 0b11111011
                mode_54 = 'or' if enable else 'and'

                mask_55 = 0b11111011 if enable else 0b00000100
                mode_55 = 'and' if enable else 'or'

                self._write_bits(0x54, 0b11111011, 'and')  # Disable GPIO2
                self._write_bits(0x55, mask_55, mode_55)   # Toggle Charge
                self._write_bits(0x54, mask_54, mode_54)   # Re-enable GPIO2

            elif self.model == 'PiSugar2Plus':
                mask_56 = 0b00000100 if enable else 0b11111011
                mode_56 = 'or' if enable else 'and'

                mask_58 = 0b11111011 if enable else 0b00000100
                mode_58 = 'and' if enable else 'or'

                self._write_bits(0x56, 0b11111011, 'and')
                self._write_bits(0x58, mask_58, mode_58)
                self._write_bits(0x56, mask_56, mode_56)
        except OSError:
            pass

    def _convert_voltage_to_level(self, curve):
        if len(self.voltage_history) < 5:
            avg_v = sum(self.voltage_history) / len(self.voltage_history)
        else:
            # Trim outliers
            sorted_v = sorted(self.voltage_history)
            trimmed = sorted_v[2:-2]
            avg_v = sum(trimmed) / len(trimmed)

        for (v1, p1), (v2, p2) in zip(curve, curve[1:]):
            if v2 <= avg_v <= v1:
                return p2 + (p1 - p2) * (avg_v - v2) / (v1 - v2)

        return curve[-1][1] if avg_v < curve[-1][0] else curve[0][1]

    def shutdown(self):
        try:
            if self.model == 'PiSugar3':
                self._write_protected(0x09, 10)  # 10s delay
                current_02 = self._bus.read_byte_data(self.address, 0x02)
                self._write_protected(0x02, current_02 & 0b11011111)
            logging.info("[psugar] Hardware shutdown scheduled.")
        except Exception as e:
            logging.error(f"[psugar] Shutdown command failed: {e}")

    # --- Public Getters ---
    def get_version(self):
        if self.model == 'PiSugar3' and len(self.i2creg) > 0xee:
            return bytes(self.i2creg[0xe2:0xee]).decode('ascii', errors='ignore')
        return "Legacy"


class PiSugar(plugins.Plugin):
    __author__ = "jayofelony (cleaned by d5aint)"
    __version__ = "2.0.1"
    __license__ = "GPL3"
    __description__ = "Unified PiSugar 2/3 Battery Status & Management."
    __name__ = "PiSugar"
    __help__ = "Unified PiSugar 2/3 Battery Status & Management."
    __dependencies__ = {
        "pip": ["smbus"],
    }
    __defaults__ = {
        'rotation': True,
        'default_display': 'percentage',
        'lowpower_shutdown': True,
        'lowpower_shutdown_level': 10,
        'max_charge_voltage_protection': True,
        'bat_x_coord': None,
        'bat_y_coord': None
    }

    def __init__(self):
        self.ps = None
        self.ready = False
        self.drot = 0
        self.next_dchg = 0
        self._agent = None
        self.rotation_enabled = True
        self.default_display = 'percentage'

    def on_loaded(self):
        # Config options
        self.rotation_enabled = self.options.get('rotation', True)
        self.default_display = self.options.get('default_display', 'percentage')

        # Initialize Hardware Interface
        try:
            self.ps = PiSugarServer()
            self.ps.lowpower_shutdown = self.options.get('lowpower_shutdown', True)
            self.ps.lowpower_shutdown_level = self.options.get('lowpower_shutdown_level', 10)
            self.ps.max_charge_voltage_protection = self.options.get(
                'max_charge_voltage_protection', True
            )
            logging.info("[psugar] plugin loaded.")
        except Exception as e:
            logging.error(f"[psugar] Fatal Error: {e}")

    def on_ready(self, agent):
        self._agent = agent

    def on_ui_setup(self, ui):
        # Determine X coordinate
        if self.options.get('bat_x_coord') is not None:
            pos_x = self.options['bat_x_coord']
        else:
            pos_x = ui.width() / 2 + 15

        # Determine Y coordinate
        if self.options.get('bat_y_coord') is not None:
            pos_y = self.options['bat_y_coord']
        else:
            pos_y = 0

        ui.add_element(
            'bat',
            LabeledValue(
                color=BLACK,
                label='BAT',
                value='0%',
                position=(pos_x, pos_y),
                label_font=fonts.Bold,
                text_font=fonts.Medium
            )
        )

    def on_unload(self, ui):
        with ui._lock:
            try:
                ui.remove_element('bat')
            except Exception:
                pass

    def on_ui_update(self, ui):
        if not self.ps or not self.ps.ready:
            return

        # Fetch Data
        v = self.ps.battery_voltage
        p = self.ps.battery_level
        t = self.ps.temperature
        plugged = self.ps.power_plugged

        # Update Label (Plugged status)
        try:
            if plugged:
                ui._state._state['bat'].label = "CHG"
            else:
                ui._state._state['bat'].label = "BAT"
        except Exception:
            pass

        # Rotation Logic
        if self.rotation_enabled:
            if time.time() > self.next_dchg:
                self.drot = (self.drot + 1) % 3
                self.next_dchg = time.time() + 5

            if self.drot == 0:
                ui.set('bat', f"{p:.0f}%")
            elif self.drot == 1:
                ui.set('bat', f"{v:.2f}V")
            else:
                ui.set('bat', f"{t}°C")
        else:
            # Fixed Display
            mode = self.default_display.lower()
            if mode == 'voltage':
                ui.set('bat', f"{v:.2f}V")
            elif 'temp' in mode:
                ui.set('bat', f"{t}°C")
            else:
                ui.set('bat', f"{p:.0f}%")

    def on_webhook(self, path, request):
        if not self.ps or not self.ps.ready:
            return "<html><body><h1>PiSugar initializing...</h1></body></html>"

        if request.method == "GET":
            # Simple status page
            html = f"""
            <!DOCTYPE html>
            <html>
            <head><title>PiSugar Status</title></head>
            <body>
                <h1>PiSugar {self.ps.model}</h1>
                <table border="1" style="border-collapse: collapse; width: 300px;">
                    <tr><th>Metric</th><th>Value</th></tr>
                    <tr><td>Voltage</td><td>{self.ps.battery_voltage:.2f} V</td></tr>
                    <tr><td>Level</td><td>{self.ps.battery_level:.1f} %</td></tr>
                    <tr><td>Temp</td><td>{self.ps.temperature} °C</td></tr>
                    <tr><td>Plugged</td><td>{self.ps.power_plugged}</td></tr>
                    <tr><td>FW Version</td><td>{self.ps.get_version()}</td></tr>
                </table>
            </body>
            </html>
            """
            return render_template_string(html)
