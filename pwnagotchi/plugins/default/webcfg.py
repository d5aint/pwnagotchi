import logging
import json
import toml
import threading
import pwnagotchi
from pwnagotchi import restart, plugins
from pwnagotchi.utils import save_config, merge_config
from flask import abort, render_template_string

INDEX = """
{% extends "base.html" %}
{% set active_page = "plugins" %}
{% block title %}
    Webcfg
{% endblock %}

{% block meta %}
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, user-scalable=0" />
{% endblock %}

{% block styles %}
{{ super() }}
<style>
    #divTop {
        position: -webkit-sticky;
        position: sticky;
        top: 0px;
        width: 100%;
        font-size: 16px;
        padding: 5px;
        border: 1px solid #ddd;
        margin-bottom: 5px;
        background-color: white;
        z-index: 100;
        display: table;
    }

    #searchText {
        width: 100%;
    }

    table {
        table-layout: auto;
        width: 100%;
        border-collapse: collapse;
    }

    table, th, td {
      border: 1px solid black;
    }

    th, td {
      padding: 15px;
      text-align: left;
    }

    table tr:nth-child(even) {
      background-color: #eee;
    }

    table tr:nth-child(odd) {
     background-color: #fff;
    }

    table th {
      background-color: black;
      color: white;
    }

    .remove {
        background-color: #f44336;
        color: white;
        border: 2px solid #f44336;
        padding: 4px 8px;
        text-align: center;
        text-decoration: none;
        display: inline-block;
        font-size: 12px;
        margin: 4px 2px;
        -webkit-transition-duration: 0.4s; /* Safari */
        transition-duration: 0.4s;
        cursor: pointer;
    }

    .remove:hover {
        background-color: white;
        color: black;
    }

    #btnSave, #btnMerge {
        position: -webkit-sticky;
        position: sticky;
        bottom: 0px;
        width: 100%;
        background-color: #0061b0;
        border: none;
        color: white;
        padding: 15px 32px;
        text-align: center;
        text-decoration: none;
        display: inline-block;
        font-size: 16px;
        cursor: pointer;
        margin: 5px;
    }
    
    #divSaveTop {
        position: -webkit-sticky;
        position: sticky;
        bottom: 0px;
        width: 100%;
        background-color: white;
        border-top: 1px solid #ddd;
        padding: 10px;
        text-align: right;
        z-index: 100;
    }

    #divTop > * {
        display: table-cell;
    }
    #divTop > span {
        width: 1%;
        white-space: nowrap;
    }
    #divTop > input {
        width: 100%;
    }

    @media screen and (max-width:700px) {
        table, tr, td {
            padding: 0;
            border: 1px solid black;
        }

        table {
            border: none;
        }

        tr:first-child, thead, th {
            display: none;
            border: none;
        }

        tr {
            float: left;
            width: 100%;
            margin-bottom: 2em;
        }

        td {
            float: left;
            width: 100%;
            padding: 1em;
        }

        td::before {
            content: attr(data-label);
            word-wrap: break-word;
            background: #eee;
            border-right: 2px solid black;
            width: 20%;
            float: left;
            padding: 1em;
            font-weight: bold;
            margin: -1em 1em -1em -1em;
        }
    }
</style>
{% endblock %}

{% block content %}
    <div id="divTop">
        <input type="text" id="searchText" placeholder="Search for options ..." title="Type an option name">
        <span>
            <select id="selAddType">
                <option value="text">Text</option>
                <option value="number">Number</option>
            </select>
        </span>
        <span><button id="btnAdd" type="button" onclick="addOption()">+</button></span>
    </div>
    
    <div id="content"></div>

    <div id="divSaveTop">
        <button id="btnSave" type="button" onclick="saveConfig()">Save & Restart</button>
        <button id="btnMerge" type="button" onclick="saveConfigNoRestart()">Merge & Save (No Restart)</button>
    </div>
{% endblock %}

{% block script %}
    function addOption() {
        var input, table, tr, td, divDelBtn, btnDel, selType, selTypeVal;
        input = document.getElementById("searchText");
        var inputVal = input.value;
        selType = document.getElementById("selAddType");
        selTypeVal = selType.options[selType.selectedIndex].value;
        table = document.getElementById("tableOptions");
        
        if (table && inputVal) {
            tr = table.insertRow(1); // Insert after header
            
            // Delete button
            td = document.createElement("td");
            td.setAttribute("data-label", "");
            btnDel = document.createElement("Button");
            btnDel.innerHTML = "X";
            btnDel.onclick = function(){ delRow(this); };
            btnDel.className = "remove";
            td.appendChild(btnDel);
            tr.appendChild(td);
            
            // Option Key
            td = document.createElement("td");
            td.setAttribute("data-label", "Option");
            td.innerHTML = inputVal;
            tr.appendChild(td);
            
            // Option Value
            td = document.createElement("td");
            td.setAttribute("data-label", "Value");
            var inputElem = document.createElement("input");
            inputElem.type = selTypeVal;
            inputElem.value = "";
            td.appendChild(inputElem);
            tr.appendChild(td);

            input.value = "";
        }
    }

    function saveConfig() {
        var table = document.getElementById("tableOptions");
        if (table) {
            var json = tableToJson(table);
            sendJSON("webcfg/save-config", json, function(response) {
                if (response.status == "200") {
                    alert("Config updated. Restarting...");
                } else {
                    alert("Error updating config (Code: " + response.status + ")");
                }
            });
        }
    }

    function saveConfigNoRestart() {
        var table = document.getElementById("tableOptions");
        if (table) {
            var json = tableToJson(table);
            sendJSON("webcfg/merge-save-config", json, function(response) {
                if (response.status == "200") {
                    alert("Config merged and saved.");
                } else {
                    alert("Error merging config (Code: " + response.status + ")");
                }
            });
        }
    }

    var searchInput = document.getElementById("searchText");
    searchInput.onkeyup = function() {
        var filter = searchInput.value.toUpperCase();
        var table = document.getElementById("tableOptions");
        if (table) {
            var tr = table.getElementsByTagName("tr");
            for (var i = 1; i < tr.length; i++) { // Start at 1 to skip header
                var td = tr[i].getElementsByTagName("td")[1];
                if (td) {
                    var txtValue = td.textContent || td.innerText;
                    if (txtValue.toUpperCase().indexOf(filter) > -1) {
                        tr[i].style.display = "";
                    } else {
                        tr[i].style.display = "none";
                    }
                }
            }
        }
    }

    function sendJSON(url, data, callback) {
        var xobj = new XMLHttpRequest();
        var csrf = "{{ csrf_token() }}";
        xobj.open('POST', url);
        xobj.setRequestHeader("Content-Type", "application/json");
        xobj.setRequestHeader('x-csrf-token', csrf);
        xobj.onreadystatechange = function () {
            if (xobj.readyState == 4) {
                callback(xobj);
            }
        };
        xobj.send(JSON.stringify(data));
    }

    function loadJSON(url, callback) {
        var xobj = new XMLHttpRequest();
        xobj.overrideMimeType("application/json");
        xobj.open('GET', url, true);
        xobj.onreadystatechange = function () {
            if (xobj.readyState == 4 && xobj.status == "200") {
                callback(JSON.parse(xobj.responseText));
            }
        };
        xobj.send(null);
    }

    function unFlattenJson(data) {
        if (Object(data) !== data || Array.isArray(data)) return data;
        var result = {}, cur, prop, idx, last, temp, inarray;
        for(var p in data) {
            cur = result, prop = "", last = 0, inarray = false;
            do {
                idx = p.indexOf(".", last);
                temp = p.substring(last, idx !== -1 ? idx : undefined);
                inarray = temp.startsWith('#') && !isNaN(parseInt(temp.substring(1)));
                cur = cur[prop] || (cur[prop] = (inarray ? [] : {}));
                prop = inarray ? temp.substring(1) : temp;
                last = idx + 1;
            } while(idx >= 0);
            cur[prop] = data[p];
        }
        return result[""] || result;
    }

    function flattenJson(data) {
        var result = {};
        function recurse (cur, prop) {
            if (Object(cur) !== cur) {
                result[prop] = cur;
            } else if (Array.isArray(cur)) {
                for(var i=0, l=cur.length; i<l; i++)
                     recurse(cur[i], prop ? prop+".#"+i : ""+i);
                if (l == 0) result[prop] = [];
            } else {
                var isEmpty = true;
                for (var p in cur) {
                    isEmpty = false;
                    recurse(cur[p], prop ? prop+"."+p : p);
                }
                if (isEmpty) result[prop] = {};
            }
        }
        recurse(data, "");
        return result;
    }

    function delRow(btn) {
        var tr = btn.parentNode.parentNode;
        tr.parentNode.removeChild(tr);
    }

    function jsonToTable(json) {
        var table = document.createElement("table");
        table.id = "tableOptions";

        var tr = table.insertRow();
        ["", "Option", "Value"].forEach(function(text) {
            var th = document.createElement("th");
            th.innerHTML = text;
            tr.appendChild(th);
        });

        Object.keys(json).sort().forEach(function(key) {
            tr = table.insertRow();
            
            // Delete button
            var tdDel = document.createElement("td");
            tdDel.setAttribute("data-label", "");
            var btnDel = document.createElement("Button");
            btnDel.innerHTML = "X";
            btnDel.className = "remove";
            btnDel.onclick = function(){ delRow(this); };
            tdDel.appendChild(btnDel);
            tr.appendChild(tdDel);
            
            // Option Name
            var tdOpt = document.createElement("td");
            tdOpt.setAttribute("data-label", "Option");
            tdOpt.innerHTML = key;
            tr.appendChild(tdOpt);
            
            // Value Input
            var tdVal = document.createElement("td");
            tdVal.setAttribute("data-label", "Value");
            var input;
            
            if (typeof(json[key]) === 'boolean') {
                input = document.createElement("select");
                var optTrue = document.createElement("option");
                optTrue.value = "true";
                optTrue.text = "True";
                var optFalse = document.createElement("option");
                optFalse.value = "false";
                optFalse.text = "False";
                input.appendChild(optTrue);
                input.appendChild(optFalse);
                input.value = json[key].toString();
            } else {
                input = document.createElement("input");
                if (Array.isArray(json[key])) {
                    input.type = 'text';
                    input.value = JSON.stringify(json[key]);
                } else {
                    input.type = typeof(json[key]) === 'number' ? 'number' : 'text';
                    input.value = json[key];
                }
            }
            tdVal.appendChild(input);
            tr.appendChild(tdVal);
        });

        return table;
    }

    function tableToJson(table) {
        var rows = table.getElementsByTagName("tr");
        var json = {};

        for (var i = 1; i < rows.length; i++) { // Skip header
            var td = rows[i].getElementsByTagName("td");
            if (td.length == 3) {
                var key = td[1].textContent || td[1].innerText;
                var input = td[2].querySelector("input, select");
                
                if (input) {
                    if (input.tagName === "SELECT") {
                        json[key] = input.value === 'true';
                    } else if (input.type === "number") {
                        json[key] = Number(input.value);
                    } else {
                        var val = input.value;
                        if (val.startsWith("[") && val.endsWith("]")) {
                            try {
                                json[key] = JSON.parse(val);
                            } catch(e) {
                                json[key] = val; // Fallback
                            }
                        } else {
                            json[key] = val;
                        }
                    }
                }
            }
        }
        return unFlattenJson(json);
    }

    loadJSON("webcfg/get-config", function(response) {
        var flat_json = flattenJson(response);
        var table = jsonToTable(flat_json);
        var divContent = document.getElementById("content");
        divContent.innerHTML = "";
        divContent.appendChild(table);
    });
{% endblock %}
"""


def serializer(obj):
    if isinstance(obj, set):
        return list(obj)
    raise TypeError


class WebConfig(plugins.Plugin):
    __author__ = 'dadav'
    __version__ = "1.0.0"
    __license__ = 'GPL3'
    __description__ = 'This plugin allows the user to make runtime changes to the configuration via the Web UI.'

    def __init__(self):
        self.ready = False
        self.mode = 'MANU'
        self._agent = None
        self.config = {}

    def on_config_changed(self, config):
        self.config = config
        self.ready = True

    def on_ready(self, agent):
        self._agent = agent
        self.mode = 'MANU' if agent.mode == 'manual' else 'AUTO'

    def on_internet_available(self, agent):
        self._agent = agent
        self.mode = 'MANU' if agent.mode == 'manual' else 'AUTO'

    def on_loaded(self):
        """
        Gets called when the plugin gets loaded
        """
        logging.info("[webcfg] Plugin loaded.")

    def on_webhook(self, path, request):
        """
        Serves the current configuration
        """
        if not self.ready:
            return "Plugin not ready"

        if request.method == "GET":
            if path == "/" or not path:
                return render_template_string(INDEX)
            elif path == "get-config":
                return json.dumps(self.config, default=serializer)
            else:
                abort(404)
        
        elif request.method == "POST":
            if path == "save-config":
                try:
                    new_config = request.get_json()
                    logging.info("[webcfg] Saving config and restarting...")
                    save_config(new_config, '/etc/pwnagotchi/config.toml')
                    
                    # Use threading instead of _thread
                    threading.Thread(target=restart, args=(self.mode,)).start()
                    return "success"
                except Exception as ex:
                    logging.error(f"[webcfg] Save error: {ex}", exc_info=True)
                    return "config error", 500
            
            elif path == "merge-save-config":
                try:
                    new_data = request.get_json()
                    
                    # Merge logic
                    self.config = merge_config(new_data, self.config)
                    pwnagotchi.config = merge_config(new_data, pwnagotchi.config)
                    
                    if self._agent:
                        self._agent._config = merge_config(new_data, self._agent._config)
                        logging.debug(f"[webcfg] Agent config updated: {self._agent._config}")

                    logging.info("[webcfg] Merging and saving config (no restart)...")
                    save_config(new_data, '/etc/pwnagotchi/config.toml')
                    return "success"
                except Exception as ex:
                    logging.error(f"[webcfg] Merge error: {ex}", exc_info=True)
                    return "config error", 500
        
        abort(404)
