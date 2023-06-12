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
from datetime import datetime as dt
from pprint import pprint as pp

import requests
import tenacity
from dotenv import load_dotenv
from tesla_powerwall.error import APIError
from tesla_powerwall.powerwall import Powerwall
from tesla_powerwall.const import MeterType
from tesla_powerwall.responses import Meter, Battery


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

# end environment variables

# constants
# URL to post to
URL = 'https://metric-api.newrelic.com/metric/v1'

# header to go with it
HEADER = {
    'Content-Type': 'application/json',
    'Api-Key': INSIGHTS_API_KEY,
}
# end constants

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


@tenacity.retry(reraise=True,
                stop=tenacity.stop_after_attempt(1),
                wait=tenacity.wait_random(min=3, max=7))
def post_metrics(data):
    """POST a block of data and headers to a URL."""

    response = requests.post(URL, json=[data], headers=HEADER)
    status = response.status_code
    if status == 202:
        return 0
    else:
        raise Exception(f'return code is {status}')

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

    batteries: list[Battery] = []
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


def make_meter_gauges(name: str, meter: Meter, invertDirection: bool = False, type: str = 'gauge') -> list[dict]:
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


def run_from_cli():
    """Print data and exit. Useful when running the script from the CLI."""
    pp(data, compact=True)
    timestamp = data['common']['timestamp']
    print(f'timestamp:\t{timestamp:_}')
    sys.exit(0)


if __name__ == "__main__":
    # If POLL_INTERVAL is a multiple of a minute, try to start at the beginning of the next minute
    if POLL_INTERVAL % 60 == 0 and AS_SERVICE:
        wait_time = 60 - time.localtime().tm_sec
        print('Found minute intervals, delaying first iteration',
              wait_time, 'seconds until the start of the next minute')
        print()
        time.sleep(wait_time)

    while True:
        start = time.time()
        try:
            data = get_data()
            #ret = post_metrics(data)

            print('Submitted at', dt.now())
        except APIError as apiEx:
            print(apiEx)
            # If this is an HTTP 429, back off immediately for at least 5 minutes
            if str(apiEx).find('429: Too Many Requests') > 0:
                FIVE_MINUTES = 5 * 60
                elapsed = time.time() - start
                # Back off for at least 3x POLL_INTERVAL, for a minimum of 5 minutes to allow things to cool down
                backoffInterval = POLL_INTERVAL * 3
                if backoffInterval < FIVE_MINUTES:
                    backoffInterval = FIVE_MINUTES
                print('Backing off for', round(backoffInterval - elapsed, 0), 'seconds because of HTTP 429.')
                time.sleep(backoffInterval - elapsed)
                # Determine if we need to wait until the start of the minute again
                if POLL_INTERVAL % 60 == 0 and AS_SERVICE:
                    wait_time = 60 - time.localtime().tm_sec
                    time.sleep(wait_time)
                    # Reset the start time to coincide with the top of the minute
                    start = time.time()
        except Exception as ex:
            print('Failed to gather data:', ex)

        if not AS_SERVICE:
            run_from_cli()

        # Try to position each loop exactly POLL_INTERVAL seconds apart.
        # This is most useful when POLL_INTERVAL is an even division of a minute
        elapsed = time.time() - start
        if elapsed < 0 or elapsed > POLL_INTERVAL:
            elapsed = 0
        time.sleep(POLL_INTERVAL - elapsed)
