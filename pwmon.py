#!/usr/bin/env python3
"""
Push solar and related data to New Relic.
Can be run from the CLI or as a service. If running as a service, set the 
environment variable AS_SERVICE to something.
"""

import os
import sys
import time
from datetime import datetime as dt
from pprint import pprint as pp

import requests
import tenacity
from dotenv import load_dotenv
from tesla_powerwall.powerwall import Powerwall
from tesla_powerwall.const import MeterType
from tesla_powerwall.responses import Meter


##### environment variables
# load environment variables from a file if they're there
load_dotenv('env.list', override=False)

# this script expects these environment variables to be set
# New Relic key
INSIGHTS_API_KEY = os.environ.get('INSIGHTS_API_KEY')

# Weather lat/long and key
WEATHER_LAT = os.environ.get('WEATHER_LAT')
WEATHER_LON = os.environ.get('WEATHER_LON')
WEATHER_KEY = os.environ.get('WEATHER_KEY')

# powerwall username
PW_USER = os.environ.get('PW_USER')

# powerwall password
PW_PASS = os.environ.get('PW_PASS')

# Am I running as a service?  Part of a hack to let me run via CLI.
AS_SERVICE = os.environ.get('AS_SERVICE')

# How often does the script poll when run as a service?
POLL_INTERVAL = int(os.environ.get('POLL_INTERVAL'))

# powerwall hostname or IP.
# The powerwall's self-signed certificate only responds to 
# hostnamnes "powerwall", "teg", or "powerpack", and of course you have to have DNS set up properly.
# IP addresses work, too.
PW_ADDR =  os.environ.get("PW_ADDR")

##### end environment variables

##### constants
# URL to post to
URL = 'https://metric-api.newrelic.com/metric/v1'

# header to go with it
HEADER = {
    'Content-Type': 'application/json',
    'Api-Key': INSIGHTS_API_KEY,
}
##### end constants


def get_now():
    """Return the current Unix timestamp in msec."""
    return int(dt.timestamp(dt.today()) * 1000)


@tenacity.retry(stop=tenacity.stop_after_attempt(1),
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

@tenacity.retry(stop=tenacity.stop_after_attempt(7),
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


@tenacity.retry(stop=tenacity.stop_after_attempt(7),
                wait=tenacity.wait_random(min=3, max=7))
def get_weather():
    """Return weather for a given lat/lon."""
    params = {
        'lat': WEATHER_LAT,
        'lon': WEATHER_LON,
        'appid': WEATHER_KEY,
        'units': 'imperial',
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
                "mode": pw.get_operation_mode().name.title(),
                "status": pw.get_grid_status().name.title(),
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
        is_daytime = True
    else:
        is_daytime = False

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

    reserve = make_gauge('solar.reserve_pct',
                         pw.get_backup_reserve_percentage())
    data['metrics'].append(reserve)

    tmp = round(pw.get_charge(), 1)
    remaining = make_gauge(
        'solar.pct_left_above_reserve', int(
            tmp - pw.get_backup_reserve_percentage()))
    data['metrics'].append(remaining)

    return data

def make_meter_gauges(name:str, meter:Meter, invertDirection:bool=False, type:str='gauge') -> list:
    """Return a list of gauges for a supplied Meter"""
    gauges = [
        make_gauge('solar.to_' + name, 0, type),
        make_gauge('solar.from_' + name, 0, type)
    ]

    activeGauge = 1 if meter.instant_power > 0 and not invertDirection else 0
    gauges[activeGauge]['value'] = abs(meter.instant_power)

    return gauges


def make_gauge(name, value, m_type='gauge'):
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
    while True:
        data = get_data()
        ret = post_metrics(data)

        print('submitted at', dt.now(), "return code", ret)
        if not AS_SERVICE:
            run_from_cli()
        time.sleep(POLL_INTERVAL)
