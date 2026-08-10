#!/usr/bin/env python3
"""
Push solar and related data to New Relic.
Can be run from the CLI or as a service. If running as a service, set the 
environment variable AS_SERVICE to something.
"""

import os
import sys
import time
import enum
import logging
from abc import ABC, abstractmethod
from datetime import datetime as dt
from pprint import pprint as pp

import requests
import tenacity
from dotenv import load_dotenv
from prometheus_client import Gauge, CollectorRegistry, start_http_server
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

# tesla_powerwall is pinned in requirements.txt: 0.4.x is the last sync API
# (0.5.0+ is async/aiohttp and would require rewriting all Powerwall calls)
from tesla_powerwall import (
    ApiError,
    BatteryResponse,
    MeterResponse,
    MeterType,
    Powerwall,
)


# environment variables
# load environment variables from a file if they're there
load_dotenv('env.list', override=False)

# this script expects these environment variables to be set
# New Relic key
INSIGHTS_API_KEY = os.environ.get('INSIGHTS_API_KEY', '')

# Weather lat/long, units, and key
WEATHER_LAT = os.environ.get('WEATHER_LAT', 0)
WEATHER_LON = os.environ.get('WEATHER_LON', 0)
WEATHER_UNITS = os.environ.get('WEATHER_UNITS', 'imperial')
WEATHER_KEY = os.environ.get('WEATHER_KEY', '')

# powerwall username
PW_USER = os.environ.get('PW_USER', '')

# powerwall password
PW_PASS = os.environ.get('PW_PASS', '')

# Am I running as a service?  Part of a hack to let me run via CLI.
AS_SERVICE = os.environ.get('AS_SERVICE', '')

# How often does the script poll when run as a service?
POLL_INTERVAL = int(os.environ.get('POLL_INTERVAL', 60))

# powerwall hostname or IP.
# The powerwall's self-signed certificate only responds to
# hostnamnes "powerwall", "teg", or "powerpack", and of course you have to have DNS set up properly.
# IP addresses work, too.
PW_ADDR = os.environ.get("PW_ADDR", 'powerwall')

# Optional Metrics
#   Reserve Percent (enabled by default)
#   Reserve Percent Available (enabled by default)
#   Battery Charge in Wh
#   Battery Capacity in Wh
#   Grid Status as Gauge
OPT_RESERVE_PCT = os.environ.get('OPT_RESERVE_PCT', True)
OPT_RESERVE_PCT_AVAIL = os.environ.get('OPT_RESERVE_PCT_AVAIL', True)
OPT_BATTERY_CHARGE_WH = os.environ.get('OPT_BATTERY_CHARGE_WH', False)
OPT_BATTERY_CAPACITY_WH = os.environ.get('OPT_BATTERY_CAPACITY_WH', False)
OPT_GRID_STATUS_GAUGE = os.environ.get('OPT_GRID_STATUS_GAUGE', False)

# Multi-target export configuration
EXPORTERS = os.environ.get('EXPORTERS', 'newrelic')

# Prometheus configuration
PROMETHEUS_PORT = int(os.environ.get('PROMETHEUS_PORT', 9090))

# InfluxDB v2 configuration
INFLUXDB_URL = os.environ.get('INFLUXDB_URL', '')
INFLUXDB_TOKEN = os.environ.get('INFLUXDB_TOKEN', '')
INFLUXDB_ORG = os.environ.get('INFLUXDB_ORG', '')
INFLUXDB_BUCKET = os.environ.get('INFLUXDB_BUCKET', '')

# end environment variables

# Grid Status Enum for OPT_GRID_STATUS_GAUGE


class GridStatus(enum.IntEnum):
    UNKNOWN = 0
    CONNECTED = 1
    ISLANDED_READY = 2
    ISLANDED = 3
    TRANSITION_TO_GRID = 4
    TRANSITION_TO_ISLAND = 5

    def _missing(self, value):
        return self.UNKNOWN
# end Grid Status Enum


def get_now():
    """Return the current Unix timestamp in msec."""
    return int(time.time() * 1000)


# tenacity is only really useful for pw
#  because the gateway is very slow to respond
#  and it has some absurdly low rate limit


@tenacity.retry(reraise=True,
                stop=tenacity.stop_after_attempt(7),
                wait=tenacity.wait_random(min=3, max=7))
def get_pw():
    """Return a Powerwall connection object."""
    pw = Powerwall(PW_ADDR)
    loginResult = pw.login(PW_PASS, PW_USER)
    return pw


def connect():
    """Return a Powerwall object and its meters."""
    pw = get_pw()
    return pw, pw.get_meters()


@tenacity.retry(reraise=True,
                stop=tenacity.stop_after_attempt(7),
                wait=tenacity.wait_random(min=3, max=7))
def get_weather():
    """Return weather for a given lat/lon."""
    params = {
        'lat': WEATHER_LAT,
        'lon': WEATHER_LON,
        'appid': WEATHER_KEY,
        'units': WEATHER_UNITS,
    }
    response = requests.get(
        url="http://api.openweathermap.org/data/2.5/weather", params=params)
    r = response.json()
    return r


def get_data():
    """Return powerwall and weather data formatted for submission as New Relic metrics."""
    now = get_now()

    # ought to do these two in an event loop but weather is so fast it's not
    # worth it.
    pw, m = connect()

    # Get a copy of each meter
    batteryMeter = m.get_meter(MeterType.BATTERY)
    loadMeter = m.get_meter(MeterType.LOAD)
    siteMeter = m.get_meter(MeterType.SITE)
    solarMeter = m.get_meter(MeterType.SOLAR)

    weather = get_weather()

    data = {
        "common": {
            "timestamp": now,
            "interval.ms": POLL_INTERVAL * 1000,
            "attributes": {
                "app.name": "solar",
                "mode": pw.get_operation_mode().name.title().replace('_', ' '),
                "status": pw.get_grid_status().name.title().replace('_', ' '),
                "poll_timestamp": now,
            }
        },
        "metrics": [],

    }

    # figure out if the sun is up.  This is helpful
    # when trying to know how much power to expect from the panels.

    weather['sys']['sunrise'] *= 1000
    weather['sys']['sunset'] *= 1000
    if now > weather['sys']['sunrise'] and now < weather['sys']['sunset']:
        is_daytime = 1
    else:
        is_daytime = 0

    metric_data = {
        'solar': [
            ('battery_charge_pct', round(pw.get_charge(), 1)),
            ('battery.imported', batteryMeter.energy_imported),
            ('battery.exported', batteryMeter.energy_exported),
            ('house.imported', loadMeter.energy_imported),
            ('house.exported', loadMeter.energy_exported),
            ('grid.imported', siteMeter.energy_imported),
            ('grid.exported', siteMeter.energy_exported),
            ('solar.imported', solarMeter.energy_imported),
            ('solar.exported', solarMeter.energy_exported),
        ],
        'weather': [
            ('cloud_coverage_pct', weather['clouds']['all']),
            ('visibility', weather['visibility']),
            ('temperature', weather['main']['temp']),
            ('is_daytime', is_daytime),
        ]
    }

    # turn stuff into weather.stuff and solar.stuff.
    #  not very useful for solar because so much of that is bespoke
    for k, v_list in metric_data.items():
        for pair in v_list:
            m_name = pair[0]
            try:
                m_value = pair[1]
            except KeyError:
                m_value = 0
            m_name = f'{k}.{m_name}'
            data['metrics'].append(make_gauge(m_name, m_value))

    data['metrics'].extend(make_meter_gauges('solar', solarMeter))
    data['metrics'].extend(make_meter_gauges('grid', siteMeter))
    # The Load/House meter is inverted (e.g. positive is "to" and negative is "from")
    data['metrics'].extend(make_meter_gauges('house', loadMeter, True))
    data['metrics'].extend(make_meter_gauges('battery', batteryMeter))

    # Add optional metrics
    #   Reserve Percent (enabled by default)
    #   Reserve Percent Available (enabled by default)
    #   Battery Charge in Wh
    #   Battery Capacity in Wh
    #   Grid Status

    if OPT_RESERVE_PCT:
        reserve = make_gauge('solar.reserve_pct',
                             pw.get_backup_reserve_percentage())
        data['metrics'].append(reserve)

    if OPT_RESERVE_PCT_AVAIL:
        tmp = round(pw.get_charge(), 1)
        remaining = make_gauge(
            'solar.pct_left_above_reserve', int(
                tmp - pw.get_backup_reserve_percentage()))
        data['metrics'].append(remaining)

    batteries: list[BatteryResponse] = []
    if OPT_BATTERY_CHARGE_WH or OPT_BATTERY_CAPACITY_WH:
        batteries = pw.get_batteries()

    if OPT_BATTERY_CHARGE_WH:
        tmp = 0
        for battery in batteries:
            tmp = tmp + battery.energy_remaining

        charge_Wh = make_gauge('solar.battery_charge_wh', tmp)
        data['metrics'].append(charge_Wh)

    if OPT_BATTERY_CAPACITY_WH:
        tmp = 0
        for battery in batteries:
            tmp = tmp + battery.capacity

        capacity = make_gauge('solar.battery_capacity_wh', tmp)
        data['metrics'].append(capacity)

    if OPT_GRID_STATUS_GAUGE:
        grid_status = make_gauge(
            'solar.grid_status', GridStatus[pw.get_grid_status().name].value)
        data['metrics'].append(grid_status)

    return data


def make_meter_gauges(name: str, meter: MeterResponse, invertDirection: bool = False, type: str = 'gauge') -> list[dict]:
    """Return a list of gauges for a supplied Meter"""
    gauges = [
        make_gauge('solar.to_' + name, 0, type),
        make_gauge('solar.from_' + name, 0, type)
    ]

    activeGauge = 1 if meter.instant_power > 0 and not invertDirection else 0
    gauges[activeGauge]['value'] = abs(meter.instant_power)

    return gauges


def make_gauge(name: str, value: int | float, m_type: str = 'gauge') -> dict:
    """Return a dict for use as a gauge."""
    return {
        'name': name,
        'value': value,
        'type': m_type
    }


def run_from_cli(data):
    """Print data and exit. Useful when running the script from the CLI."""
    pp(data, compact=True)
    timestamp = data['common']['timestamp']
    logger.info('timestamp:\t%s', timestamp)
    sys.exit(0)


# Multi-Target Export Architecture

class BaseExporter(ABC):
    """Abstract base class for metric exporters."""

    @abstractmethod
    def validate_config(self):
        """Validate required env vars. Raise ValueError if missing."""
        pass

    @abstractmethod
    def export(self, data: dict):
        """Transform and export metrics."""
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        """Return exporter name for logging."""
        pass


class NewRelicExporter(BaseExporter):
    """Export metrics to New Relic via Metrics API."""

    def __init__(self):
        self.url = 'https://metric-api.newrelic.com/metric/v1'
        self.header = {
            'Content-Type': 'application/json',
            'Api-Key': INSIGHTS_API_KEY,
        }

    @property
    def name(self) -> str:
        return "newrelic"

    def validate_config(self):
        """Validate New Relic API key is configured."""
        if not INSIGHTS_API_KEY:
            raise ValueError("INSIGHTS_API_KEY not configured")

    @tenacity.retry(reraise=True,
                    stop=tenacity.stop_after_attempt(1),
                    wait=tenacity.wait_random(min=3, max=7))
    def export(self, data: dict):
        """POST metrics to New Relic."""
        response = requests.post(self.url, json=[data], headers=self.header)
        status = response.status_code
        if status != 202:
            raise Exception(f'return code is {status}')


class PrometheusExporter(BaseExporter):
    """Export metrics to Prometheus via HTTP /metrics endpoint."""

    def __init__(self):
        self.registry = CollectorRegistry()
        self.gauges = {}  # metric name -> Gauge object
        self.http_server_started = False
        # Gauges keep their last value between scrapes, so a dead Powerwall
        # poll is invisible to `up`; alert on this timestamp going stale.
        self.last_success = Gauge(
            'pwmon_last_success_timestamp_seconds',
            'Unix time of the last successful collect-and-export cycle',
            registry=self.registry)
        # mode/status ride as info-style metrics rather than labels on every
        # gauge: labels would fork every series' identity on each mode change
        # and leave stale label combinations behind.
        self.mode_info = Gauge(
            'pwmon_operation_mode_info',
            'Current Powerwall operation mode (value is always 1)',
            labelnames=['mode'],
            registry=self.registry)
        self.grid_status_info = Gauge(
            'pwmon_grid_status_info',
            'Current grid status (value is always 1)',
            labelnames=['status'],
            registry=self.registry)

    @property
    def name(self) -> str:
        return "prometheus"

    def validate_config(self):
        """Validate Prometheus port is configured."""
        if not PROMETHEUS_PORT:
            raise ValueError("PROMETHEUS_PORT not configured")

    def export(self, data: dict):
        """Update Prometheus gauges and start HTTP server if needed."""
        # Start HTTP server on first export (lazy init)
        if not self.http_server_started:
            start_http_server(PROMETHEUS_PORT, registry=self.registry)
            self.http_server_started = True
            logger.info(f'Prometheus HTTP server started on port {PROMETHEUS_PORT}')

        # clear() drops stale label values so only the current mode/status
        # series exists at any scrape
        common_attrs = data['common']['attributes']
        self.mode_info.clear()
        self.mode_info.labels(mode=common_attrs.get('mode', '')).set(1)
        self.grid_status_info.clear()
        self.grid_status_info.labels(status=common_attrs.get('status', '')).set(1)

        # Update or create gauges for each metric
        for metric in data['metrics']:
            metric_name = metric['name'].replace('.', '_')

            if metric_name not in self.gauges:
                self.gauges[metric_name] = Gauge(
                    metric_name,
                    f'pwmon metric {metric["name"]}',
                    registry=self.registry
                )

            self.gauges[metric_name].set(metric['value'])

        self.last_success.set_to_current_time()


class InfluxDBExporter(BaseExporter):
    """Export metrics to InfluxDB v2 via line protocol."""

    def __init__(self):
        self.client = None
        self.write_api = None

    @property
    def name(self) -> str:
        return "influxdb"

    def validate_config(self):
        """Validate InfluxDB credentials and initialize client."""
        if not INFLUXDB_URL:
            raise ValueError("INFLUXDB_URL not configured")
        if not INFLUXDB_TOKEN:
            raise ValueError("INFLUXDB_TOKEN not configured")
        if not INFLUXDB_ORG:
            raise ValueError("INFLUXDB_ORG not configured")
        if not INFLUXDB_BUCKET:
            raise ValueError("INFLUXDB_BUCKET not configured")

        # Initialize client
        self.client = InfluxDBClient(
            url=INFLUXDB_URL,
            token=INFLUXDB_TOKEN,
            org=INFLUXDB_ORG
        )
        self.write_api = self.client.write_api(write_options=SYNCHRONOUS)

    @tenacity.retry(reraise=True,
                    stop=tenacity.stop_after_attempt(3),
                    wait=tenacity.wait_random(min=1, max=3))
    def export(self, data: dict):
        """Convert to InfluxDB line protocol and write."""
        timestamp = data['common']['timestamp'] * 1000000  # Convert ms to ns
        common_attrs = data['common']['attributes']

        points = []
        for metric in data['metrics']:
            # Parse metric name: 'solar.battery_charge_pct' -> measurement='solar', field='battery_charge_pct'
            parts = metric['name'].split('.', 1)
            if len(parts) == 2:
                measurement, field = parts
            else:
                measurement = 'pwmon'
                field = metric['name']

            point = Point(measurement) \
                .tag('mode', common_attrs.get('mode', '')) \
                .tag('status', common_attrs.get('status', '')) \
                .field(field, metric['value']) \
                .time(timestamp)

            points.append(point)

        # Write all points in batch
        self.write_api.write(bucket=INFLUXDB_BUCKET, record=points)


def get_enabled_exporters() -> list[BaseExporter]:
    """Return list of enabled exporters based on EXPORTERS env var."""
    exporters_str = EXPORTERS
    exporter_classes = {
        'newrelic': NewRelicExporter,
        'prometheus': PrometheusExporter,
        'influxdb': InfluxDBExporter,
    }
    exporters = []
    for name in exporters_str.split(','):
        name = name.strip().lower()
        if name in exporter_classes:
            try:
                exporter = exporter_classes[name]()
                exporter.validate_config()
                exporters.append(exporter)
                logger.info(f'Enabled exporter: {exporter.name}')
            except ValueError as e:
                logger.error(f'{name}: {e}')
                sys.exit(1)
        else:
            logger.warning(f'Unknown exporter: {name}')
    return exporters

logging.basicConfig(format='%(asctime)s %(name)s.%(funcName)s %(levelname)s: %(message)s',
                    datefmt='[%Y-%m-%d %H:%M:%S]', level=logging.INFO)
logger: logging.Logger = logging.getLogger('pwmon')

if __name__ == "__main__":
    logger.info('Startup')
    exporters = get_enabled_exporters()

    try:
        # If POLL_INTERVAL is a multiple of a minute, try to start at the beginning of the next minute
        if POLL_INTERVAL % 60 == 0 and AS_SERVICE:
            wait_time = 60 - time.localtime().tm_sec
            logger.info('Found minute intervals, delaying first iteration %s seconds until the start of the next minute', wait_time)
            time.sleep(wait_time)

        while True:
            start = time.time()
            try:
                data = get_data()

                # Export to all enabled targets
                for exporter in exporters:
                    try:
                        exporter.export(data)
                        logger.info(f'{exporter.name}: export successful')
                    except Exception as ex:
                        logger.warning(f'{exporter.name}: export failed - {ex}')
                        logger.exception(ex)

                logger.info('Submitted at %s', dt.now())
            except ApiError as apiEx:
                logger.warning(apiEx)
                # If this is an HTTP 429, back off immediately for at least 5 minutes
                if str(apiEx).find('429: Too Many Requests') > 0:
                    FIVE_MINUTES = 5 * 60
                    elapsed = time.time() - start
                    # Back off for at least 3x POLL_INTERVAL, for a minimum of 5 minutes to allow things to cool down
                    backoffInterval = POLL_INTERVAL * 3
                    if backoffInterval < FIVE_MINUTES:
                        backoffInterval = FIVE_MINUTES
                    logger.info('Backing off for %s seconds because of HTTP 429.', round(backoffInterval - elapsed, 0))
                    time.sleep(backoffInterval - elapsed)
                    # Determine if we need to wait until the start of the minute again
                    if POLL_INTERVAL % 60 == 0 and AS_SERVICE:
                        wait_time = 60 - time.localtime().tm_sec
                        time.sleep(wait_time)
                        # Reset the start time to coincide with the top of the minute
                        start = time.time()
            except (SystemExit, KeyboardInterrupt) as ex:
                    logger.info('%s received; shutting down...',
                                ex.__class__.__name__)
                    break
            except Exception as ex:
                logger.warning('Failed to gather data: %s', ex)
                logger.exception(ex)

            if not AS_SERVICE:
                run_from_cli(data)

            # Try to position each loop exactly POLL_INTERVAL seconds apart.
            # This is most useful when POLL_INTERVAL is an even division of a minute
            elapsed = time.time() - start
            if elapsed < 0 or elapsed > POLL_INTERVAL:
                elapsed = 0
            time.sleep(POLL_INTERVAL - elapsed)
    except (SystemExit, KeyboardInterrupt) as ex:
        logger.info('%s received; shutting down...',
                    ex.__class__.__name__)
    except Exception as ex:
        logger.warning('Exception during main loop: %s', ex)
        logger.exception(ex)
    logger.info('Shutdown')
