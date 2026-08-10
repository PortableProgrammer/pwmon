# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`pwmon` is a Python monitoring tool that collects Tesla Powerwall and weather data and sends it to New Relic as metrics. The entire application is a single Python script (`pwmon.py`) designed to run either as a one-shot CLI command or as a continuous service in a Docker container.

## Running the Application

### From CLI (one-time execution)
```bash
# Set environment variables from file and run
export $(grep -v '^#' env.list | xargs)
./pwmon.py
```

### As a Docker Container (continuous service)
```bash
# Build the image
docker build -t pwmon .

# Run with environment file
docker run --env-file=env.list pwmon
```

### Development Container
A devcontainer configuration exists for VS Code. After opening in the devcontainer, dependencies are automatically installed via `pip3 install --user -r requirements.txt`.

## Configuration

All configuration is done via environment variables defined in `env.list`:

- **New Relic**: `INSIGHTS_API_KEY` for metric submission
- **Powerwall**: `PW_ADDR`, `PW_USER`, `PW_PASS` for API access
- **Weather**: `WEATHER_LAT`, `WEATHER_LON`, `WEATHER_KEY`, `WEATHER_UNITS` for OpenWeatherMap API
- **Service Mode**: `AS_SERVICE` (set to any value to enable continuous polling)
- **Polling**: `POLL_INTERVAL` (seconds between data collections, default 60)
- **Optional Metrics**: `OPT_RESERVE_PCT`, `OPT_RESERVE_PCT_AVAIL`, `OPT_BATTERY_CHARGE_WH`, `OPT_BATTERY_CAPACITY_WH`, `OPT_GRID_STATUS_GAUGE`
- **Multi-target Export**: `EXPORTERS` (comma-separated list: `newrelic`, `prometheus`, `influxdb`)
  - Prometheus: `PROMETHEUS_PORT` (HTTP server port for /metrics endpoint)
  - InfluxDB: `INFLUXDB_URL`, `INFLUXDB_TOKEN`, `INFLUXDB_ORG`, `INFLUXDB_BUCKET`

## Architecture

### Main Flow (pwmon.py:~480-550)

1. **Startup Alignment**: If `POLL_INTERVAL` is a multiple of 60 and `AS_SERVICE` is set, the script delays the first iteration to start at the top of the next minute
2. **Data Collection Loop**:
   - `get_data()` collects Powerwall and weather data
   - Iterate through enabled exporters and call their `export()` methods
   - Sleep until next interval (accounting for elapsed time)
3. **Error Handling**:
   - HTTP 429 rate limit: backs off for 3x `POLL_INTERVAL` or 5 minutes (whichever is longer)
   - Other exceptions: logs error and continues service loop
   - Exporter failures are isolated (one exporter failure doesn't crash others)
4. **CLI Mode**: If `AS_SERVICE` is not set, prints data and exits after one iteration

### Data Collection (pwmon.py:154-277)

- **Powerwall Connection**: Uses `tesla_powerwall` library with tenacity retry (7 attempts, 3-7 second random wait)
- **Meter Data**: Collects from BATTERY, LOAD, SITE, and SOLAR meters
- **Weather Data**: Fetches from OpenWeatherMap API with same retry logic
- **Metric Structure**: All data formatted as New Relic gauge metrics with common attributes (mode, status, timestamp)
- **Directional Power Flow**: Meters report bidirectional power as `to_X` and `from_X` gauges (pwmon.py:280-290)
  - Load/House meter is inverted (positive = to house, negative = from house)

### Multi-Target Export (pwmon.py:~310-470)

- **Plugin Architecture**: `BaseExporter` abstract class with concrete implementations
- **Exporters**:
  - `NewRelicExporter`: Push to New Relic Metrics API (existing behavior)
  - `PrometheusExporter`: Updates in-memory gauges, serves HTTP `/metrics` endpoint via prometheus-client library
  - `InfluxDBExporter`: Converts to line protocol and pushes to InfluxDB v2 API
- **Error Isolation**: Each exporter wrapped in try/except; single failure doesn't crash others
- **Lazy Initialization**: Prometheus HTTP server starts on first export (avoids binding port if not used)

### Retry Strategy

The Powerwall API has aggressive rate limiting. The code uses `tenacity` library:
- **Powerwall operations**: 7 retry attempts with 3-7 second random waits
- **New Relic exporter**: 1 attempt with 3-7 second wait
- **InfluxDB exporter**: 3 attempts with 1-3 second wait
- **Rate limit detection**: Catches HTTP 429 errors and implements exponential backoff

### Grid Status Enum

When `OPT_GRID_STATUS_GAUGE` is enabled, grid status is converted from string to integer using `GridStatus` enum (pwmon.py:85-94):
- UNKNOWN = 0
- CONNECTED = 1
- ISLANDED_READY = 2
- ISLANDED = 3
- TRANSITION_TO_GRID = 4
- TRANSITION_TO_ISLAND = 5

## CI/CD

GitHub Actions workflow (`.github/workflows/docker-image.yml`) builds multi-architecture Docker images:
- **Platforms**: linux/amd64, linux/arm/v6, linux/arm/v7, linux/arm64
- **Triggers**: PRs to main, pushes to main, version tags (v*)
- **Registry**: Pushes to Docker Hub as `portableprogrammer/pwmon`
- **Tagging**: Version tags get both `vX.Y.Z` and `latest`, other builds tagged as `edge`

## Dependencies

- `tenacity`: Retry logic for rate-limited Powerwall API
- `tesla_powerwall`: Tesla Powerwall API client library
- `python-dotenv`: Load environment variables from files
- `requests`: HTTP client for weather API and New Relic submission
- `prometheus-client`: Prometheus metrics registry and HTTP server for /metrics endpoint
- `influxdb-client`: InfluxDB v2 API client for time-series data export
