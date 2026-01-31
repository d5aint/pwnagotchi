import logging
import threading
from flask import render_template_string, Response, abort
import pwnagotchi.plugins as plugins

TEMPLATE = """
{% extends "base.html" %}
{% set active_page = "plugins" %}
{% block title %}
    Logtail
{% endblock %}

{% block styles %}
    {{ super() }}
    <style>
        * {
            box-sizing: border-box;
        }
        #filter {
            width: 100%;
            font-size: 16px;
            padding: 12px 20px 12px 40px;
            border: 1px solid #ddd;
            margin-bottom: 12px;
        }
        table {
            border-collapse: collapse;
            width: 100%;
            border: 1px solid #ddd;
        }
        th, td {
            text-align: left;
            padding: 12px;
            width: 1px;
            white-space: nowrap;
        }
        td:nth-child(2) {
            text-align: center;
        }
        thead, tr:hover {
            background-color: #f1f1f1;
        }
        tr {
            border-bottom: 1px solid #ddd;
        }
        div.sticky {
            position: -webkit-sticky;
            position: sticky;
            top: 0;
            display: table;
            width: 100%;
            background: white; /* Ensure background is opaque */
            z-index: 100;
        }
        div.sticky > * {
            display: table-cell;
        }
        div.sticky > span {
            width: 1%;
            white-space: nowrap;
            padding-left: 10px;
        }
        div.sticky > input {
            width: 100%;
        }
        tr.default {
            color: black;
        }
        tr.info {
            color: black;
        }
        tr.warning {
            color: darkorange;
        }
        tr.error {
            color: crimson;
        }
        tr.debug {
            color: blueviolet;
        }
        .ui-mobile .ui-page-active {
            overflow: visible;
            overflow-x: visible;
        }
    </style>
{% endblock %}

{% block script %}
    var table = document.getElementById('table');
    var filter = document.getElementById('filter');
    var filterVal = filter.value.toUpperCase();

    var xhr = new XMLHttpRequest();
    xhr.open('GET', '{{ url_for('plugins') }}/logtail/stream');
    xhr.send();
    var position = 0;
    var data;
    var time;
    var level;
    var msg;
    var colorClass;

    function handleNewData() {
        var messages = xhr.responseText.split('\\n');
        filterVal = filter.value.toUpperCase();
        
        // Only process new messages
        messages.slice(position, -1).forEach(function(value) {
            var msg, time, level, data;

            if (value.charAt(0) != '[') {
                msg = value;
                time = '';
                level = '';
                colorClass = 'default';
            } else {
                data = value.split(']');
                // Basic parsing assuming format [TIME] [LEVEL] MESSAGE
                if (data.length >= 2) {
                    time = data.shift() + ']';
                    level = data.shift() + ']';
                    msg = data.join(']');
                } else {
                    msg = value;
                    time = '';
                    level = '';
                }

                switch(level) {
                    case ' [INFO]':
                        colorClass = 'info';
                        break;
                    case ' [WARNING]':
                        colorClass = 'warning';
                        break;
                    case ' [ERROR]':
                        colorClass = 'error';
                        break;
                    case ' [DEBUG]':
                        colorClass = 'debug';
                        break;
                    default:
                        colorClass = 'default';
                        break;
                }
            }

            var tr = document.createElement('tr');
            var td1 = document.createElement('td');
            var td2 = document.createElement('td');
            var td3 = document.createElement('td');

            td1.textContent = time;
            td2.textContent = level;
            td3.textContent = msg;

            tr.appendChild(td1);
            tr.appendChild(td2);
            tr.appendChild(td3);

            tr.className = colorClass;

            if (filterVal.length > 0 && value.toUpperCase().indexOf(filterVal) == -1) {
                tr.style.display = "none";
            }

            table.appendChild(tr);
        });
        position = messages.length - 1;
    }

    var scrollingElement = (document.scrollingElement || document.body);
    function scrollToBottom () {
       scrollingElement.scrollTop = scrollingElement.scrollHeight;
    }

    var timer;
    var scrollElm = document.getElementById('autoscroll');
    
    timer = setInterval(function() {
        handleNewData();
        if (scrollElm.checked) {
            scrollToBottom();
        }
        if (xhr.readyState == XMLHttpRequest.DONE) {
            clearInterval(timer);
        }
    }, 1000);

    var typingTimer;
    var doneTypingInterval = 1000;

    filter.onkeyup = function() {
        clearTimeout(typingTimer);
        typingTimer = setTimeout(doneTyping, doneTypingInterval);
    };

    filter.onkeydown = function() {
        clearTimeout(typingTimer);
    };

    function doneTyping() {
        document.body.style.cursor = 'progress';
        filterVal = filter.value.toUpperCase();
        var tr = table.getElementsByTagName("tr");
        
        // Start from 1 to skip header
        for (var i = 1; i < tr.length; i++) {
            var txtValue = tr[i].textContent || tr[i].innerText;
            if (txtValue.toUpperCase().indexOf(filterVal) > -1) {
                tr[i].style.display = "table-row";
            } else {
                tr[i].style.display = "none";
            }
        }
        document.body.style.cursor = 'default';
    }
{% endblock %}

{% block content %}
    <div class="sticky">
        <input type="text" id="filter" placeholder="Search for ..." title="Type in a filter">
        <span><input checked type="checkbox" id="autoscroll"></span>
        <span><label for="autoscroll"> Autoscroll</label></span>
    </div>
    <table id="table">
        <thead>
            <tr>
                <th>Time</th>
                <th>Level</th>
                <th>Message</th>
            </tr>
        </thead>
    </table>
{% endblock %}
"""


class Logtail(plugins.Plugin):
    __author__ = 'dadav'
    __version__ = '0.1.1'
    __license__ = 'GPL3'
    __description__ = 'This plugin tails the logfile in the web interface.'
    __defaults__ = {
        'max-lines': 4096
    }

    def __init__(self):
        self.lock = threading.Lock()
        self.options = dict()
        self.ready = False
        self.config = None

    def on_config_changed(self, config):
        self.config = config
        self.ready = True

    def on_loaded(self):
        """
        Gets called when the plugin gets loaded
        """
        logging.info("[logtail] Plugin loaded.")

    def on_webhook(self, path, request):
        if not self.ready:
            return "Plugin not ready"

        if not path or path == "/":
            return render_template_string(TEMPLATE)

        if path == 'stream':
            def generate():
                log_path = self.config['main']['log']['path']
                try:
                    with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
                        # Initial read of last N lines
                        max_lines = self.options.get('max-lines', 4096)
                        lines = f.readlines()
                        yield ''.join(lines[-max_lines:])
                        
                        # Move to end of file
                        f.seek(0, 2)
                        
                        while True:
                            line = f.readline()
                            if line:
                                yield line
                            else:
                                # Small sleep to prevent high CPU usage while waiting for logs
                                import time
                                time.sleep(0.1)
                except Exception as e:
                    logging.error(f"[logtail] Error reading log: {e}")
                    yield f"[ERROR] Could not read log file: {e}"

            return Response(generate(), mimetype='text/plain')

        abort(404)
