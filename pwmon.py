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
from tesla_powerwall import Powerwall, MeterType


# this script expects four environment variables are set
# New Relic key
INSIGHTS_API_KEY = os.environ.get('INSIGHTS_API_KEY')

# ZIPSTRING is for the wether API.  In the US it takes the form '<ZIPCODE>, us'
ZIPSTRING = f"{os.environ.get('ZIP')},us"
WEATHER_KEY = os.environ.get("WEATHER_KEY")


# Am I running as a service?  Part of a hack to let me run via CLI.
AS_SERVICE = os.environ.get('AS_SERVICE')

# How often does the script poll when run as a service?
POLL_INTERVAL = 60

# powerwall password
PW_PASS = os.environ.get("PW_PASS")

# powerwall hostname or IP.
# The powerwall's self-signed certificate only responds to 
# hostnamnes "powerwall", "teg", or "powerpack", and of course you have to have DNS set up properly.
# IP addresses work, too.
PW_ADDR = 'powerwall'

# URL to post to
URL = 'https://metric-api.newrelic.com/metric/v1'

# header to go with it
HEADER = {
    'Content-Type': 'application/json',
    'Api-Key': INSIGHTS_API_KEY,
}


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
    pw.login(PW_PASS)
    return pw


def connect():
    """Return a Powerwall object and its meters."""
    pw = get_pw()
    return pw, pw.get_meters()


@tenacity.retry(stop=tenacity.stop_after_attempt(7),
                wait=tenacity.wait_random(min=3, max=7))
def get_weather():
    """Return weather for a given zipstring."""
    params = {
        'zip': ZIPSTRING,
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

    # # WHY DO I NEED TO DO THIS?  I SWEAR THIS USED TO WORK.
    m.battery = m.get_meter(MeterType.BATTERY)
    m.load = m.get_meter(MeterType.LOAD)
    m.site = m.get_meter(MeterType.SITE)
    m.solar = m.get_meter(MeterType.SOLAR)

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
        is_sun_up = True
    else:
        is_sun_up = False

    metric_data = {
        'solar': [
            ('battery_charge_pct', round(pw.get_charge(), 1)),
            ('battery.imported', m.battery.energy_imported),
            ('battery.exported', m.battery.energy_exported),
            ('house.imported', m.load.energy_imported),
            ('house.exported', m.load.energy_exported),
            ('grid.imported', m.site.energy_imported),
            ('grid.exported', m.site.energy_exported),
            ('solar.imported', m.solar.energy_imported),
            ('solar.exported', m.solar.energy_exported),
        ],
        'weather': [
            ('cloud_coverage_pct', weather['clouds']['all']),
            ('visibility', weather['visibility']),
            ('temperature', weather['main']['temp']),
            ('is_sun_up', is_sun_up),
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

    # the to_* and from_* stuff is weird
    #  positive numbers are towards the house
    #  negative numbers are away from the house
    # these four blocks should be a function

    # 'to_solar',
    # 'from_solar',
    to_solar = make_gauge('solar.to_solar', 0)
    from_solar = make_gauge('solar.from_solar', 0)

    if m.solar.instant_power > 0:
        from_solar = make_gauge('solar.from_solar', m.solar.instant_power)
    elif m.solar.instant_power < 0:
        to_solar = make_gauge('solar.to_solar', abs(m.solar.instant_power))
    data['metrics'].append(to_solar)
    data['metrics'].append(from_solar)

    # 'to_grid',
    # 'from_grid',
    to_grid = make_gauge('solar.to_grid', 0)
    from_grid = make_gauge('solar.from_grid', 0)
    if m.site.instant_power > 0:
        from_grid = make_gauge('solar.from_grid', m.site.instant_power)
    elif m.site.instant_power < 0:
        to_grid = make_gauge('solar.to_grid', abs(m.site.instant_power))
    data['metrics'].append(to_grid)
    data['metrics'].append(from_grid)

    # 'to_house',
    # 'from_house',
    to_house = make_gauge('solar.to_house', 0)
    from_house = make_gauge('solar.from_house', 0)
    if m.load.instant_power > 0:
        to_house = make_gauge('solar.to_house', m.load.instant_power)
    elif m.load.instant_power < 0:
        from_house = make_gauge('solar.from_house', abs(m.load.instant_power))
    data['metrics'].append(to_house)
    data['metrics'].append(from_house)

    # 'to_battery',
    # 'from_battery',
    to_battery = make_gauge('solar.to_battery', 0)
    from_battery = make_gauge('solar.from_battery', 0)
    if m.battery.instant_power > 0:
        from_battery = make_gauge(
            'solar.from_battery', m.battery.instant_power)
    elif m.battery.instant_power < 0:
        to_battery = make_gauge(
            'solar.to_battery', abs(m.battery.instant_power))
    data['metrics'].append(to_battery)
    data['metrics'].append(from_battery)

    reserve = make_gauge('solar.reserve_pct',
                         pw.get_backup_reserve_percentage())
    data['metrics'].append(reserve)

    tmp = round(pw.get_charge(), 1)
    remaining = make_gauge(
        'solar.pct_left_above_reserve', int(
            tmp - pw.get_backup_reserve_percentage()))
    data['metrics'].append(remaining)

    return data


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
        time.sleep(60)
