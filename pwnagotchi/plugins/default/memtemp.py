import logging
import psutil
import pwnagotchi
import pwnagotchi.plugins as plugins
import pwnagotchi.ui.fonts as fonts
from pwnagotchi.ui.components import LabeledValue, Text
from pwnagotchi.ui.view import BLACK


class MemTemp(plugins.Plugin):
    __author__ = 'xenDE (Merged & Fixed by d5aint)'
    __version__ = "1.0.2"
    __license__ = 'GPL3'
    __description__ = 'Displays memory, CPU load, disk usage, frequency, and temperature.'
    __name__ = 'MemTemp'
    __help__ = 'Displays memory, CPU load, disk usage, frequency, and temperature.'
    __dependencies__ = {
        "pip": ["psutil"],
    }
    __defaults__ = {
        'enabled': False,
        'orientation': 'horizontal',
        'scale': 'celsius',
        'fields': 'mem,cpu,temp',
        'linespacing': 6,
        'position': ''
    }

    ALLOWED_FIELDS = {
        'mem': 'mem_usage',
        'cpu': 'cpu_load',
        'temp': 'cpu_temp',
        'freq': 'cpu_freq',
        'disk': 'disk_usage'
    }

    DEFAULT_FIELDS = ['mem', 'cpu', 'temp']
    FIELD_WIDTH = 4
    LABEL_SPACING = 0

    def __init__(self):
        self.ready = False
        self.fields = []

    def on_loaded(self):
        logging.info(f"[memtemp]] Plugin loaded.")

    # --- Data Gathering Methods ---
    def mem_usage(self):
        return f"{int(psutil.virtual_memory().percent)}%"

    def cpu_load(self):
        return f"{int(psutil.cpu_percent())}%"

    def disk_usage(self):
        return f"{int(psutil.disk_usage('/').percent)}%"

    def cpu_temp(self):
        temp = pwnagotchi.temperature()
        scale = self.options.get('scale', 'celsius').lower()

        if scale == 'fahrenheit':
            temp = (temp * 9 / 5) + 32
            symbol = "F"
        elif scale == 'kelvin':
            temp = temp + 273.15
            symbol = "K"
        else:
            symbol = "C"

        return f"{int(temp)}{symbol}"

    def cpu_freq(self):
        try:
            with open('/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq', 'rt') as fp:
                return f"{round(float(fp.readline()) / 1000000, 1)}G"
        except Exception:
            return "0.0G"

    def pad_text(self, data):
        return " " * (self.FIELD_WIDTH - len(data)) + data

    # --- UI Setup ---
    def on_ui_setup(self, ui):
        # 1. Parse Fields
        try:
            raw_fields = self.options.get('fields', 'mem,cpu,temp').split(',')
            self.fields = [x.strip() for x in raw_fields if x.strip() in self.ALLOWED_FIELDS]
            # Limit to 4 to prevent UI overflow
            self.fields = self.fields[:4]
        except Exception:
            self.fields = self.DEFAULT_FIELDS

        # 2. Parse Spacing & Position
        line_spacing = int(self.options.get('linespacing', 10))

        # Default positions based on screen type
        if ui.is_waveshare_v2() or ui.is_waveshare_v3():
            default_h = (178, 84)
            default_v = (197, 70)
        elif ui.is_waveshare144lcd():
            default_h = (53, 77)
            default_v = (73, 67)
        elif ui.is_inky():
            default_h = (140, 68)
            default_v = (160, 54)
        else:  # V1 or generic
            default_h = (155, 76)
            default_v = (175, 61)

        # Allow user override
        try:
            pos_cfg = self.options.get('position', '').split(',')
            user_pos = [int(x.strip()) for x in pos_cfg] if len(pos_cfg) == 2 else None
        except Exception:
            user_pos = None

        if self.options.get('orientation') == 'vertical':
            pos = user_pos if user_pos else default_v
            # Setup Vertical UI
            for idx, field in enumerate(self.fields):
                y_offset = (len(self.fields) - 3) * -1 * line_spacing
                ui.add_element(
                    f"memtemp_{field}",
                    LabeledValue(
                        color=BLACK,
                        label=f"{self.pad_text(field)}:",
                        value="-",
                        position=(pos[0], pos[1] + y_offset + (idx * line_spacing)),
                        label_font=fonts.Small,
                        text_font=fonts.Small,
                        label_spacing=self.LABEL_SPACING,
                    )
                )
        else:
            pos = user_pos if user_pos else default_h
            # Setup Horizontal UI
            # Adjust X slightly left if we have more fields to keep it centered-ish
            x_adj = (len(self.fields) - 3) * -1 * 20
            ui.add_element(
                "memtemp_header",
                Text(
                    color=BLACK,
                    value=" ".join([self.pad_text(x) for x in self.fields]),
                    position=(pos[0] + x_adj, pos[1]),
                    font=fonts.Small,
                )
            )
            ui.add_element(
                "memtemp_data",
                Text(
                    color=BLACK,
                    value=" ".join([self.pad_text("-") for x in self.fields]),
                    position=(pos[0] + x_adj, pos[1] + line_spacing),
                    font=fonts.Small,
                )
            )

    def on_unload(self, ui):
        with ui._lock:
            try:
                if self.options.get('orientation') == 'vertical':
                    for field in self.fields:
                        ui.remove_element(f"memtemp_{field}")
                else:
                    ui.remove_element("memtemp_header")
                    ui.remove_element("memtemp_data")
            except Exception as e:
                logging.error(f"[memtemp]] Unload error: {e}")

    def on_ui_update(self, ui):
        if self.options.get('orientation') == 'vertical':
            for field in self.fields:
                val = getattr(self, self.ALLOWED_FIELDS[field])()
                ui.set(f"memtemp_{field}", val)
        else:
            # Horizontal join
            data = " ".join([
                self.pad_text(getattr(self, self.ALLOWED_FIELDS[x])())
                for x in self.fields
            ])
            ui.set("memtemp_data", data)
