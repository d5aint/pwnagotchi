import logging
import random
import time
import html
import os
import json
import glob

import pwnagotchi.plugins as plugins
import pwnagotchi.ui.fonts as fonts
import pwnagotchi.utils
from pwnagotchi.utils import save_config
from flask import render_template_string

class AutoTune(plugins.Plugin):
    __author__ = 'Sniffleupagus, modified by d5aint'
    __version__ = "1.0.1"
    __license__ = 'GPL3'
    __description__ = 'Adjusts AUTO mode parameters dynamically and provides preset management.'

    def __init__(self):
        self._histogram = {'loops': 0}
        self._chistos = {'_all_actions': {-1: 0}}

        self.ep_data = {}
        self.last_shake = {'time': time.time()}
        self._unscanned_channels = []
        self._active_channels = []
        self._known_aps = {}
        self._agent = None
        self._orig_mode = 'AUTO'
        
        # Safe directory handling
        self.presets_dir = os.path.join(os.path.expanduser("~"), "auto-tune-presets")
        self._ensure_presets_dir()

        self.descriptions = {
            "advertise": "Enable/disable advertising to mesh peers",
            "deauth": "Enable/disable deauthentication attacks",
            "associate": "Enable/disable association attacks",
            "throttle_a": "Delay (ms) after association to reduce crashes",
            "throttle_d": "Delay (ms) after deauth to reduce crashes",
            "assoc_prob": "Probability of trying an associate attack",
            "deauth_prob": "Probability of trying a deauth attack",
            "min_rssi": "Ignore APs with signal weaker than this value",
            "recon_time": "Duration of bettercap channel hopping scan phase",
            "min_recon_time": "Time spent on each occupied channel per epoch",
            "max_interactions": "Max attacks on an AP per session",
            "ap_ttl": "Ignore APs not seen for this many seconds",
            "sta_ttl": "Ignore clients older than this many seconds",
        }
        self.options = dict()

    # --- Preset Management ---
    def _ensure_presets_dir(self):
        try:
            if not os.path.exists(self.presets_dir):
                os.makedirs(self.presets_dir, mode=0o755)
                logging.info(f"[auto-tune] Created presets directory: {self.presets_dir}")
        except OSError as e:
            logging.error(f"[auto-tune] Failed to create presets directory: {e}")

    def _sanitize_filename(self, filename):
        """Prevents directory traversal"""
        return os.path.basename(filename)

    def _get_preset_files(self):
        try:
            self._ensure_presets_dir()
            files = glob.glob(os.path.join(self.presets_dir, "*.json"))
            return sorted([os.path.splitext(os.path.basename(f))[0] for f in files])
        except Exception:
            return []

    def _save_preset(self, preset_name):
        try:
            self._ensure_presets_dir()
            safe_name = self._sanitize_filename(preset_name)
            if not safe_name: 
                raise ValueError("Invalid filename")

            preset_data = {
                'personality': {k: v for k, v in self._agent._config['personality'].items() 
                              if isinstance(v, (int, str, float, bool))},
                'plugin_settings': {k: v for k, v in self.options.items() 
                                  if isinstance(v, (int, str, float, bool))},
                'timestamp': time.time()
            }
            with open(os.path.join(self.presets_dir, f"{safe_name}.json"), 'w') as f:
                json.dump(preset_data, f, indent=2)
            return True
        except Exception as e:
            logging.error(f"[auto-tune] Error saving preset: {e}")
            raise e

    def _load_preset(self, preset_name):
        safe_name = self._sanitize_filename(preset_name)
        filepath = os.path.join(self.presets_dir, f"{safe_name}.json")

        if not os.path.exists(filepath):
            return False, "Preset file not found"

        try:
            with open(filepath, 'r') as f:
                data = json.load(f)

            changes = []
            if 'personality' in data:
                for k, v in data['personality'].items():
                    if k in self._agent._config['personality']:
                        if self._agent._config['personality'][k] != v:
                            self._agent._config['personality'][k] = v
                            changes.append(f"{k}")

            if 'plugin_settings' in data:
                for k, v in data['plugin_settings'].items():
                    if k in self.options:
                        self.options[k] = v

            return True, f"Loaded preset '{safe_name}'. Updated: {', '.join(changes)}"
        except Exception as e:
            return False, str(e)

    def _delete_preset(self, preset_name):
        try:
            safe_name = self._sanitize_filename(preset_name)
            filepath = os.path.join(self.presets_dir, f"{safe_name}.json")
            if os.path.exists(filepath):
                os.remove(filepath)
                return True
            return False
        except Exception:
            return False

    # --- Statistics Helpers ---
    def incrementChisto(self, stat, channel, count=1):
        if stat not in self._chistos:
            self._chistos[stat] = {-1: 0}
        self._chistos[stat][channel] = self._chistos[stat].get(channel, 0) + count

        self._chistos['_all_actions'][channel] = self._chistos['_all_actions'].get(channel, 0) + count
        self._chistos[stat][-1] += count
        self._chistos['_all_actions'][-1] += count

    def normalize(self, name):
        if not name: return 'EMPTY'
        if name == '<hidden>': return 'HIDDEN'
        return str.lower(''.join(c for c in name if c.isalnum()))

    # --- Core Pwnagotchi Handlers ---
    def on_loaded(self):
        defaults = {
            'show_hidden': False,
            'reset_history': True,
            'extra_channels': 3,
            'show_interactions': False
        }
        for k, v in defaults.items():
            if k not in self.options:
                self.options[k] = v

    def on_ready(self, agent):
        self._agent = agent
        if self.options['reset_history']:
            try:
                self._agent._history = {}
                # Check for existence of 'run' method for safety
                if hasattr(self._agent, 'run'):
                    self._agent.run("wifi.recon clear")
                    self._agent.run("wifi.clear")
                    channels = agent._config['personality'].get('channels', [1, 6, 11])
                    self._agent.run(f"wifi.recon.channel {','.join(map(str, channels))}")
            except Exception as e:
                logging.warning(f"[auto-tune] Error resetting history: {e}")

        if agent._config.get('ai', {}).get('enabled', False):
            logging.warning("[auto-tune] AI is enabled! AutoTune will remain passive.")
        else:
            logging.info("[auto-tune] AI disabled. AutoTune taking control.")

    def on_epoch(self, agent, epoch, epoch_data):
        if agent._config.get('ai', {}).get('enabled', False):
            return

        self.ep_data = epoch_data
        self.ep_data['epoch'] = epoch

        try:
            next_channels = self._active_channels.copy()
            n = self.options.get("extra_channels", 3)

            # Refill unscanned list if empty
            if not self._unscanned_channels:
                if "restrict_channels" in self.options:
                    self._unscanned_channels = self.options["restrict_channels"].copy()
                elif hasattr(agent, "_allowed_channels"):
                    self._unscanned_channels = agent._allowed_channels.copy()
                elif hasattr(agent, "_supported_channels"):
                    self._unscanned_channels = agent._supported_channels.copy()
                else:
                    self._unscanned_channels = pwnagotchi.utils.iface_channels(agent._config['main']['iface'])

            # Safety check: ensure we actually have channels to pick from
            if self._unscanned_channels:
                # Pick N random channels
                for _ in range(n):
                    if not self._unscanned_channels: break # Stop if we run out
                    ch = random.choice(list(self._unscanned_channels))
                    self._unscanned_channels.remove(ch)
                    if ch not in next_channels:
                        next_channels.append(ch)

            # Only update if we have valid channels
            if next_channels:
                agent._config['personality']['channels'] = next_channels
                logging.debug(f"[auto-tune] Next Channels: {next_channels}")

        except Exception as e:
            logging.exception(f"[auto-tune] Epoch error: {e}")

    def on_wifi_update(self, agent, access_points):
        try:
            active_channels = []
            self._histogram["loops"] = self._histogram.get("loops", 0) + 1

            for ap in self._known_aps.values():
                ap['AT_visible'] = False

            for ap in access_points:
                self.markAPSeen(ap, 'wifi_update')
                ch = ap['channel']
                if ch < 0: continue

                if ch not in active_channels:
                    active_channels.append(ch)
                    if ch in self._unscanned_channels:
                        try:
                            self._unscanned_channels.remove(ch)
                        except ValueError:
                            pass # Channel might have been removed already
                
                self._histogram[ch] = self._histogram.get(ch, 0) + 1

            self._active_channels = active_channels
        except Exception as e:
            logging.exception(f"[auto-tune] Wifi update error: {e}")

    def markAPSeen(self, access_point, context=None):
        try:
            apname = self.normalize(access_point['hostname'])
            apmac = self.normalize(access_point['mac'])
            apID = f"{apname}-{apmac}"
            channel = access_point['channel']
            tag = f"AT_{context}" if context else 'AT_seen'

            if apID not in self._known_aps:
                self._known_aps[apID] = access_point.copy()
                self._known_aps[apID]['AT_seen'] = 1
                self._known_aps[apID][tag] = 1
                self._known_aps[apID]['AT_visible'] = True
                self.incrementChisto('Unique APs', channel)
                self.incrementChisto('Current APs', channel)
            else:
                self._known_aps[apID].update(access_point)

                if not self._known_aps[apID].get('AT_visible', False):
                    self._known_aps[apID]['AT_visible'] = True
                    self._known_aps[apID]['AT_seen'] = self._known_aps[apID].get('AT_seen', 0) + 1
                    self.incrementChisto('Current APs', channel)

                self._known_aps[apID][tag] = self._known_aps[apID].get(tag, 0) + 1

            self._known_aps[apID]['AT_lastseen'] = time.time()
            return True
        except Exception:
            return False

    # --- UI & Display Handlers ---
    def on_ui_setup(self, ui):
        self._ui = ui
        self._orig_mode = ui.get('mode')
        if self._orig_mode != 'MANU':
            ui.set('mode', 'AT')

        # Safe state access
        try:
            if hasattr(ui, '_state') and hasattr(ui._state._state.get('mode'), 'set_click_url'):
                ui._state._state['mode'].set_click_url('/plugins/auto_tune')
        except Exception:
            pass

    def on_ui_update(self, ui):
        if self._orig_mode == 'MANU': return

        # Update Mode Indicator
        mode = f"E{self.ep_data.get('epoch', 'ST')}|{int(self.ep_data.get('duration_secs', 0))}s"
        ui.set('mode', mode)

        # Update Shakes Timer
        if self._agent and self._agent._last_pwnd:
            lt = int(time.time() - self.last_shake.get('time', time.time()))
            if lt >= 3600:
                time_str = f"@{int(lt/3600)}:{int((lt%3600)/60):02d}"
            elif lt >= 100:
                time_str = f"@{int(lt/60)}m{lt%60:02d}"
            else:
                time_str = f"@{lt}s"

            # Format: Handshakes / Unique (Last_Pwnd Time_Since)
            unique_shakes = 0
            if hasattr(self._agent, '_total_u_shakes'):
                unique_shakes = self._agent._total_u_shakes
            elif hasattr(pwnagotchi.utils, 'total_unique_handshakes'):
                unique_shakes = pwnagotchi.utils.total_unique_handshakes(self._agent._config['bettercap']['handshakes'])

            # Handle standard list or set for handshakes
            total_shakes = len(self._agent._handshakes)
            last_pwnd_clean = str(self._agent._last_pwnd)[:15].strip()

            shakes_display = f"{total_shakes}/{unique_shakes} {last_pwnd_clean} {time_str}"
            ui.set('shakes', shakes_display)

    def on_unload(self, ui):
        if self._orig_mode:
            ui.set('mode', self._orig_mode)
        try:
            if hasattr(ui, '_state') and hasattr(ui._state._state.get('mode'), 'set_click_url'):
                ui._state._state['mode'].set_click_url('http://pwnagotchi.org')
        except Exception:
            pass

    # --- Attack Handlers ---
    def on_association(self, agent, access_point):
        self.incrementChisto('Associations', access_point['channel'])
        self.markAPSeen(access_point, "assoc")

    def on_deauthentication(self, agent, access_point, client_station):
        self.incrementChisto('Deauths', access_point['channel'])
        self.markAPSeen(access_point, "deauth")

    def on_handshake(self, agent, filename, access_point, client_station):
        self.incrementChisto('Handshakes', access_point['channel'])
        self.markAPSeen(access_point, "handshake")
        self.last_shake = {'time': time.time(), 'ap': access_point, 'cl': client_station}

    # --- Web UI Helpers ---
    def update_parameter(self, cfg, parameter, vtype, val):
        if parameter not in cfg: return False
        
        old_val = cfg[parameter]
        try:
            if vtype == "int": new_val = int(val)
            elif vtype == "float": new_val = float(val)
            elif vtype == "bool": new_val = (str(val).lower() == "true")
            else: new_val = str(val)
        except ValueError:
            return False
        
        if old_val != new_val:
            cfg[parameter] = new_val
            return True
        return False

    # --- Webhook Handler ---
    def on_webhook(self, path, request):
        if not self._agent:
            return "<html><body><h1>Agent not ready</h1></body></html>"

        # HTML Template stored separate for cleanliness
        # Note: We use Jinja2 safe constructs now instead of f-strings for HTML
        HTML_TEMPLATE = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>AutoTune</title>
            <style>
                body { font-family: sans-serif; padding: 20px; }
                .preset-box { background: #f4f4f4; padding: 15px; border-radius: 5px; margin-bottom: 20px; border: 1px solid #ddd; }
                .msg-success { color: green; background: #e8f5e9; padding: 10px; border: 1px solid green; margin: 10px 0; }
                .msg-error { color: red; background: #ffebee; padding: 10px; border: 1px solid red; margin: 10px 0; }
                table { border-collapse: collapse; width: 100%; margin-bottom: 20px; }
                th { text-align: left; padding: 8px; background: #eee; border: 1px solid #ddd; }
                td { padding: 8px; border: 1px solid #ddd; }
                input[type=text] { padding: 4px; }
                .section-header { margin-top: 30px; border-bottom: 2px solid #ccc; }
            </style>
        </head>
        <body>
            <h1>AutoTune Control</h1>
            
            {% if message %}
                <div class="{{ msg_class }}">{{ message }}</div>
            {% endif %}

            <form method="post" action="{{ request.path }}">
                <input id="csrf_token" name="csrf_token" type="hidden" value="{{ csrf_token() }}">
                
                <div class="preset-box">
                    <h3>Preset Management</h3>
                    <table>
                        <tr>
                            <td>Name:</td>
                            <td><input type="text" name="preset_name" placeholder="Preset Name"></td>
                            <td><input type="submit" name="save_preset" value="Save Current Config"></td>
                        </tr>
                        <tr>
                            <td>Load/Delete:</td>
                            <td>
                                <select name="selected_preset">
                                    <option value="">Select Preset...</option>
                                    {% for p in presets %}
                                        <option value="{{ p }}">{{ p }}</option>
                                    {% endfor %}
                                </select>
                            </td>
                            <td>
                                <input type="submit" name="load_preset" value="Load">
                                <input type="submit" name="delete_preset" value="Delete" onclick="return confirm('Are you sure?')">
                            </td>
                        </tr>
                    </table>
                </div>

                {% for title, data_dict in sections %}
                    <h2 class="section-header">{{ title }}</h2>
                    <table>
                        <tr><th>Param</th><th>Value</th><th>Description</th></tr>
                        {% for key, val in data_dict.items()|sort %}
                            <tr>
                                <th>{{ key }}</th>
                                <td>
                                    {% set iname = "newval," ~ val ~ "," ~ key ~ "," ~ val|to_type %}
                                    {% if val is boolean %}
                                        <input type="radio" name="{{ iname }}" value="True" {% if val %}checked{% endif %}> True
                                        <input type="radio" name="{{ iname }}" value="False" {% if not val %}checked{% endif %}> False
                                    {% else %}
                                        <input type="text" name="{{ iname }}" value="{{ val }}" size="10">
                                    {% endif %}
                                </td>
                                <td>{{ descriptions.get(key, '') }}</td>
                            </tr>
                        {% endfor %}
                    </table>
                {% endfor %}

                <br><input type="submit" name="update_params" value="Update Configuration">
            </form>

            <h2>Channel Statistics</h2>
            <table>
                <tr>
                    <th>Channel</th>
                    {% for ch in channels %}
                        <th>{{ ch if ch != -1 else 'All' }}</th>
                    {% endfor %}
                </tr>
                {% for stat, data in chistos.items() %}
                    <tr>
                        <th>{{ stat }}</th>
                        {% for ch in channels %}
                            <td align="right">{{ data.get(ch, '-') }}</td>
                        {% endfor %}
                    </tr>
                {% endfor %}
            </table>
        </body>
        </html>
        """

        # Custom filter for Jinja to get type name
        def to_type(value):
            return type(value).__name__

        message = ""
        msg_class = ""

        if request.method == "POST":
            if 'save_preset' in request.values:
                name = request.values.get('preset_name')
                if name:
                    try:
                        self._save_preset(name)
                        message = f"Saved preset: {html.escape(name)}"
                        msg_class = "msg-success"
                    except Exception as e:
                        message = f"Error saving: {e}"
                        msg_class = "msg-error"
            
            elif 'load_preset' in request.values:
                name = request.values.get('selected_preset')
                if name:
                    success, msg = self._load_preset(name)
                    message = msg
                    msg_class = "msg-success" if success else "msg-error"
                    if success: save_config(self._agent._config, "/etc/pwnagotchi/config.toml")

            elif 'delete_preset' in request.values:
                name = request.values.get('selected_preset')
                if name and self._delete_preset(name):
                    message = f"Deleted preset: {html.escape(name)}"
                    msg_class = "msg-success"

            # Handle Params
            changed = False
            for key, val in request.values.items():
                if key.startswith('newval,'):
                    try:
                        parts = key.split(',', 3)
                        if len(parts) == 4:
                            _, _, param, vtype = parts

                            if param in self._agent._config['personality']:
                                if self.update_parameter(self._agent._config['personality'], param, vtype, val):
                                    changed = True
                            elif param in self.options:
                                if self.update_parameter(self.options, param, vtype, val):
                                    changed = True
                    except Exception as e:
                        logging.error(f"[auto-tune] Param update error: {e}")

            if changed:
                save_config(self._agent._config, "/etc/pwnagotchi/config.toml")
                if not message:
                    message = "Configuration updated and saved."
                    msg_class = "msg-success"

        # Prepare data for template
        sorted_channels = sorted([k for k in self._chistos['_all_actions'].keys()], 
                               key=lambda x: self._chistos['_all_actions'][x], reverse=True)
        
        sections = [
            ("Personality", {k: v for k, v in self._agent._config['personality'].items() 
                           if isinstance(v, (int, str, float, bool))}),
            ("Plugin Options", self.options)
        ]

        # Use Jinja2 environment correctly to prevent XSS and SSTI
        return render_template_string(HTML_TEMPLATE, 
                                    request=request,
                                    message=message,
                                    msg_class=msg_class,
                                    presets=self._get_preset_files(),
                                    sections=sections,
                                    descriptions=self.descriptions,
                                    chistos=self._chistos,
                                    channels=sorted_channels,
                                    to_type=to_type)
