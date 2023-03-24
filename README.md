# pwmon

_forked from [eosborne-newrelic/pwmon](https://github.com/eosborne-newrelic/pwmon)_

`pwmon.py` is a quick and dirty python script to demonstrate how to pull information from the Tesla Powerwall API and push it up to New Relic. I use it on my home solar system. If you want to use the weather bits you need to create an account at [openweathermap.org](https://openweathermap.org/).  

## Repo Contents

The only file you need is `pwmon.py`.

The other files to make it easy to run in a container. This is how I do it.

`Dockerfile` is the Dockerfile. Not much to say about this one.

`Makefile` is because I like shortcuts.  `make image` and `make container` are the two rules you want.

`env.list` is the list of environment variables `pwmon.py` wants to see in order to run.  
If you want to run `pwmon.py` from the CLI, one [good trick](https://stackoverflow.com/q/19331497) is

```shell
export $(grep -v '^#' env.list | xargs)
pwmon.py
```

`requirements.txt` pulls in `tenacity` (a retry library - the Powerwall API has some atrociously low rate-limit), `tesla_powerwall`, and `python-dotenv`.

This code is licensed under Apache 2.0. Essentially - do what you want with it but neither I nor New Relic are to be held liable for it. This is hacked-together demo code and it works for me at home but that's as far as I've taken it.

## Usage

### From the CLI

```shell
user@host pwmon % export $(grep -v '^#' env.list | xargs)
user@host pwmon % ./pwmon.py
submitted at 2022-01-11 08:57:20.332818 return code 0
{'common': {'attributes': {'app.name': 'solar',
                           'mode': 'Self_Consumption',
                           'poll_timestamp': 1641909439380,
                           'status': 'Connected'},
...
```

### From Docker

Build the image:

```shell
user@host:~/prog/pwmon$ make image
.....
REPOSITORY   TAG       IMAGE ID       CREATED        SIZE
pwmon        latest    de412555d252   17 hours ago 277MB                                                                                                                                      
```

Start a container:

```shell
user@host:~/prog/pwmon$ make container
docker run -d --restart unless-stopped --name pwmon --env-file env.list pwmon
974d312084d6ab79b65813dc485d18f3a110bcadc73967480b9273d9eadf3da5
```

Check that it works (you get one log line per minute):

```shell
user@host:~/prog/pwmon$ docker logs pwmon
submitted at 2022-01-11 14:00:42.418491 return code 0
```

## Getting data from New Relic

### Querying your data

There's a lot you can do once your data is in New Relic.  Like, a lot.  One sample query to get you started:

`FROM Metric SELECT average(solar.battery_charge_pct) TIMESERIES SINCE 1 week ago`

which looks like this:
![battery charge query](images/charge_pct.png)

Another sample query to build the `Power Distribution` chart, which displays all power flow for today, from the included [dashboard](README.md#creating-a-dashboard):

`FROM Metric SELECT average(solar.from_solar) AS 'From Solar', average(solar.from_grid) AS 'From Grid', average(solar.from_battery) AS 'From Battery', average(solar.to_house) AS 'House Load', average(solar.to_battery) * -1 AS 'To Battery', average(solar.to_grid) * -1 AS 'To Grid' TIMESERIES 5 minutes SINCE today WITH TIMEZONE 'America/Denver'`

which looks like this:
![power distribution](images/power_distribution.png)

### Creating a dashboard

A sample [dashboard](assets/dashboard.json) to get you started is included with `pwmon.py`. To import it, change the `accountId` fields from the dummy value of _9999999_ to your New Relic Account Id, change the [timezone](https://docs.newrelic.com/docs/query-your-data/nrql-new-relic-query-language/get-started/nrql-syntax-clauses-functions/#sel-timezone) clauses (e.g. `WITH TIMEZONE 'xxx/yyy'`), and import it on the [dashboard](https://one.newrelic.com/dashboards) page (see the [docs](https://docs.newrelic.com/docs/query-your-data/explore-query-data/dashboards/introduction-dashboards/#dashboards-import)).

___Important:___ This dashboard requires the [Victory Charts](https://github.com/newrelic/nr1-victory-visualizations) Visualizations. Without them, the `Renewable Percent` and `Battery Charge` circular progress bars will not load.

![dashboard](images/dashboard.png)
