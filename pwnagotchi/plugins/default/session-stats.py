import os
import time
import logging
import threading
from datetime import datetime, timedelta

from flask import render_template_string, jsonify
import pwnagotchi.plugins as plugins
from pwnagotchi.utils import StatusFile

TEMPLATE = """
{% extends "base.html" %}
{% set active_page = "plugins" %}
{% block title %}
    Session stats
{% endblock %}

{% block styles %}
    {{ super() }}
    <link rel="stylesheet" href="/css/jquery.jqplot.min.css"/>
    <link rel="stylesheet" href="/css/jquery.jqplot.css"/>
    <style>
        div.chart {
            height: 400px;
            width: 100%;
        }
        div#session {
            width: 100%;
        }
    </style>
{% endblock %}

{% block scripts %}
    {{ super() }}
     <script type="text/javascript" src="/js/jquery.jqplot.min.js"></script>
     <script type="text/javascript" src="/js/jquery.jqplot.js"></script>
     <script type="text/javascript" src="/js/plugins/jqplot.mobile.js"></script>
     <script type="text/javascript" src="/js/plugins/jqplot.json2.js"></script>
     <script type="text/javascript" src="/js/plugins/jqplot.dateAxisRenderer.js"></script>
     <script type="text/javascript" src="/js/plugins/jqplot.highlighter.js"></script>
     <script type="text/javascript" src="/js/plugins/jqplot.cursor.js"></script>
     <script type="text/javascript" src="/js/plugins/jqplot.enhancedLegendRenderer.js"></script>
{% endblock %}

{% block script %}
    $(document).ready(function(){
        var ajaxDataRenderer = function(url, plot, options) {
        var ret = null;
        $.ajax({
            async: false,
            url: url,
            dataType:"json",
            success: function(data) {
                ret = data;
            }
        });
        return ret;
        };

    function loadFiles(url, elm) {
        var data = ajaxDataRenderer(url);
        var x = document.getElementById(elm);
        $.each(data['files'], function( index, value ) {
            var option = document.createElement("option");
            option.text = value;
            x.add(option);
        });
    }

    function loadData(url, elm, title, fill) {
        var data = ajaxDataRenderer(url);
        var plot_os = $.jqplot(elm, data.values,{
        title: title,
        stackSeries: fill,
        seriesDefaults: {
            showMarker: !fill,
            fill: fill,
            fillAndStroke: fill
        },
        legend: {
            show: true,
            renderer: $.jqplot.EnhancedLegendRenderer,
            placement: 'outsideGrid',
            labels: data.labels,
            location: 's',
            rendererOptions: {
                numberRows: '2',
            },
            rowSpacing: '0px'
        },
        axes:{
            xaxis:{
                renderer:$.jqplot.DateAxisRenderer,
                tickOptions:{formatString:'%H:%M:%S'}
            },
            yaxis:{
                tickOptions:{formatString:'%.2f'}
            }
        },
        highlighter: {
            show: true,
            sizeAdjust: 7.5
        },
        cursor:{
            show: true,
            tooltipLocation:'sw'
        }
        }).replot({
        axes:{
            xaxis:{
                renderer:$.jqplot.DateAxisRenderer,
                tickOptions:{formatString:'%H:%M:%S'}
            },
            yaxis:{
                tickOptions:{formatString:'%.2f'}
            }
        }
        });
    }

    function loadSessionFiles() {
        loadFiles('/plugins/session-stats/session', 'session');
        $("#session").change(function() {
            loadSessionData();
        });
    }

    function loadSessionData() {
        var x = document.getElementById("session");
        var session = x.options[x.selectedIndex].text;
        loadData('/plugins/session-stats/os' + '?session=' + session, 'chart_os', 'OS', false)
        loadData('/plugins/session-stats/temp' + '?session=' + session, 'chart_temp', 'Temp', false)
        loadData('/plugins/session-stats/wifi' + '?session=' + session, 'chart_wifi', 'Wifi', true)
        loadData('/plugins/session-stats/duration' + '?session=' + session, 'chart_duration', 'Sleeping', true)
        loadData('/plugins/session-stats/reward' + '?session=' + session, 'chart_reward', 'Reward', false)
        loadData('/plugins/session-stats/epoch' + '?session=' + session, 'chart_epoch', 'Epochs', false)
    }

    loadSessionFiles();
    loadSessionData();
    setInterval(loadSessionData, 60000);
    });
{% endblock %}

{% block content %}
    <select id="session">
        <option selected>Current</option>
    </select>
    <div id="chart_os" class="chart"></div>
    <div id="chart_temp" class="chart"></div>
    <div id="chart_wifi" class="chart"></div>
    <div id="chart_duration" class="chart"></div>
    <div id="chart_reward" class="chart"></div>
    <div id="chart_epoch" class="chart"></div>
{% endblock %}
"""


class GhettoClock:
    """
    A clock that tracks time relative to plugin start.
    Useful for systems without an RTC to ensure graphs progress linearly
    even if the system time jumps.
    Replaces original threaded counter with efficient monotonic math.
    """
    def __init__(self):
        self._start_time = datetime.now()
        self._start_mono = time.monotonic()

    def now(self):
        elapsed = time.monotonic() - self._start_mono
        return self._start_time + timedelta(seconds=elapsed)


class SessionStats(plugins.Plugin):
    __author__ = 'dadav'
    __version__ = "0.1.0"
    __license__ = 'GPL3'
    __description__ = 'This plugin displays stats of the current session.'
    __defaults__ = {
        'save_directory': '/root/sessions'
    }

    def __init__(self):
        self.lock = threading.Lock()
        self.options = dict()
        self.stats = dict()
        self.clock = GhettoClock()
        self.session = None
        self.session_name = None

    def on_loaded(self):
        """
        Gets called when the plugin gets loaded
        """
        save_dir = self.options['save_directory']
        if not os.path.exists(save_dir):
            try:
                os.makedirs(save_dir, exist_ok=True)
            except OSError as e:
                logging.error(f"[session-stats] Failed to create directory {save_dir}: {e}")
                return

        timestamp = self.clock.now().strftime("%Y_%m_%d_%H_%M")
        self.session_name = f"stats_{timestamp}.json"
        session_path = os.path.join(save_dir, self.session_name)
        
        self.session = StatusFile(session_path, data_format='json')
        logging.info("[session-stats] Plugin loaded.")

    def on_epoch(self, agent, epoch, epoch_data):
        """
        Save the epoch_data to self.stats
        """
        if not self.session:
            return

        with self.lock:
            current_time = self.clock.now().strftime("%H:%M:%S")
            self.stats[current_time] = epoch_data
            self.session.update(data={'data': self.stats})

    @staticmethod
    def extract_key_values(data, subkeys):
        result = dict()
        result['values'] = list()
        result['labels'] = subkeys
        for plot_key in subkeys:
            # Handle cases where key might be missing in older data
            v = [[ts, d.get(plot_key, 0)] for ts, d in data.items()]
            result['values'].append(v)
        return result

    def on_webhook(self, path, request):
        if not path or path == "/":
            return render_template_string(TEMPLATE)

        session_param = request.args.get('session')
        extract_keys = []

        if path == "os":
            extract_keys = ['cpu_load', 'mem_usage']
        elif path == "temp":
            extract_keys = ['temperature']
        elif path == "wifi":
            extract_keys = [
                'missed_interactions',
                'num_hops',
                'num_peers',
                'tot_bond',
                'avg_bond',
                'num_deauths',
                'num_associations',
                'num_handshakes',
            ]
        elif path == "duration":
            extract_keys = [
                'duration_secs',
                'slept_for_secs',
            ]
        elif path == "reward":
            extract_keys = ['reward']
        elif path == "epoch":
            extract_keys = ['active_for_epochs']
        elif path == "session":
            try:
                files = sorted(os.listdir(self.options['save_directory']), reverse=True)
                return jsonify({'files': files})
            except FileNotFoundError:
                return jsonify({'files': []})

        with self.lock:
            data = self.stats
            # Load historical data if requested
            if session_param and session_param != 'Current':
                try:
                    file_path = os.path.join(self.options['save_directory'], session_param)
                    if os.path.exists(file_path):
                        file_stats = StatusFile(file_path, data_format='json')
                        data = file_stats.data_field_or('data', default=dict())
                except Exception as e:
                    logging.error(f"[session-stats] Error loading session {session_param}: {e}")
            
            return jsonify(SessionStats.extract_key_values(data, extract_keys))
