# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# NOTE: This module runs as a STANDALONE stdio subprocess (see
# app/agent.py:WEATHER_SERVER_PATH and `python mcp_servers/weather_server.py`).
# In that mode the project root is NOT on sys.path, so the `app` package cannot
# be imported. Therefore this file MUST remain self-contained and must NOT import
# from `app.*`. The resilient logic (timeout + retry-with-backoff + graceful
# fallback) is inlined below, mirroring app/utils/resilient.py which the
# in-process callers (/api/weather, geocode, model calls) continue to use.

import asyncio
import http.client
import json
import logging
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

from fastmcp import FastMCP
from pydantic import BaseModel, Field

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("weather_server")

# Initialize FastMCP server
mcp = FastMCP("Weather Server")

_UNAVAILABLE_WEATHER_MESSAGE = (
    "I can't reach the weather service right now, please try again shortly."
)


class WeatherForecastDay(BaseModel):
    date: str
    temp_max_c: float
    temp_min_c: float
    precip_mm: float
    weathercode: int = Field(
        description="WMO weather code for the day (e.g. 0=clear sky, 61=moderate rain, 95=thunderstorm)."
    )

class CurrentWeather(BaseModel):
    temp_c: float
    humidity_pct: float
    wind_kmh: float
    precip_mm: float

class WeatherResult(BaseModel):
    current: CurrentWeather
    recent_precip_mm_2d: float
    forecast: list[WeatherForecastDay]
    status: str = Field(
        default="ok",
        description="'ok' when the data is live; 'unavailable' when the weather service could not be reached and a graceful fallback is returned.",
    )
    message: Optional[str] = Field(
        default=None,
        description="Human-readable note; populated when status is 'unavailable'.",
    )


# ---------------------------------------------------------------------------
# Inlined resilient helpers (self-contained; no `app` dependency).
# ---------------------------------------------------------------------------

def _redact_coords(text: str) -> str:
    """Redacts lat/lon coordinates from strings before they hit the logs."""
    if not isinstance(text, str):
        text = str(text)
    text = re.sub(r'-?\d+\.\d{3,},\s*-?\d+\.\d{3,}', '[REDACTED_COORDS]', text)
    text = re.sub(r'(latitude|longitude|lat|lon)=[-?]?\d+\.\d+', r'\1=[REDACTED_COORD]', text)
    return text


def _is_transient_error(e: Exception) -> bool:
    """Returns True if the exception is a transient/retryable network/API failure."""
    if isinstance(e, urllib.error.HTTPError):
        return 500 <= e.code < 600
    if isinstance(e, urllib.error.URLError):
        return True
    if isinstance(e, (socket.timeout, TimeoutError, asyncio.TimeoutError)):
        return True
    if isinstance(e, (ConnectionError, ConnectionRefusedError, ConnectionResetError)):
        return True
    if isinstance(e, http.client.HTTPException):
        return True

    msg = str(e).upper()
    if "503" in msg or "UNAVAILABLE" in msg or "TIMEOUT" in msg or "RATE" in msg:
        return True
    return False


class _Unavailable:
    """Typed sentinel returned by the resilient runner when a call fails."""
    def __init__(self, message: str, error: Optional[Exception] = None):
        self.message = message
        self.error = error


def _run_resilient(
    func: Callable[..., Any],
    *args: Any,
    _timeout: float = 5.0,
    _max_attempts: int = 3,
    _initial_delay: float = 0.5,
    **kwargs: Any,
) -> Any:
    """Runs ``func`` with retry-with-backoff on transient errors.

    Returns the function's result on success, or an ``_Unavailable`` sentinel
    once retries are exhausted or a non-transient error occurs. Never raises for
    errors originating in ``func`` — it returns the sentinel instead. (The
    per-request timeout is enforced by the underlying urlopen call in
    ``_fetch_weather_raw``.)
    """
    attempt = 0
    delay = _initial_delay

    while attempt < _max_attempts:
        attempt += 1
        try:
            return func(*args, **kwargs)
        except Exception as e:
            func_name = getattr(func, "__name__", str(func))
            if _is_transient_error(e) and attempt < _max_attempts:
                logger.warning(
                    f"Transient error on attempt {attempt} running {func_name}: "
                    f"{_redact_coords(str(e))}. Retrying in {delay}s..."
                )
                time.sleep(delay)
                delay *= 2
            else:
                logger.error(
                    f"Hard failure or exhausted retries running {func_name}: "
                    f"{_redact_coords(str(e))}"
                )
                return _Unavailable(_UNAVAILABLE_WEATHER_MESSAGE, error=e)

    # Unreachable given _max_attempts >= 1, but keep the type contract explicit.
    return _Unavailable(_UNAVAILABLE_WEATHER_MESSAGE)


def _fetch_weather_raw(latitude: float, longitude: float) -> dict:
    """Helper to perform the raw HTTP request to Open-Meteo API."""
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,precipitation",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,weathercode",
        "past_days": 2,
        "forecast_days": 3,
        "timezone": "auto"
    }
    query_string = urllib.parse.urlencode(params)
    url = f"https://api.open-meteo.com/v1/forecast?{query_string}"

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "LeafyBot/1.0 (Weather Server)"}
    )
    # Query API with a 5-second timeout
    with urllib.request.urlopen(req, timeout=5) as response:
        if response.status == 200:
            return json.loads(response.read().decode("utf-8"))
        raise Exception(f"API returned status code {response.status}")


def _fetch_weather(latitude: float, longitude: float) -> Optional[dict]:
    """Resiliently fetches weather data. Returns the raw dict, or None if the
    service is unavailable after retries."""
    result = _run_resilient(_fetch_weather_raw, latitude, longitude)
    if isinstance(result, _Unavailable):
        return None
    return result


def _unavailable_result() -> WeatherResult:
    """A valid WeatherResult-shaped fallback flagged as unavailable."""
    return WeatherResult(
        current=CurrentWeather(temp_c=0.0, humidity_pct=0.0, wind_kmh=0.0, precip_mm=0.0),
        recent_precip_mm_2d=0.0,
        forecast=[],
        status="unavailable",
        message=_UNAVAILABLE_WEATHER_MESSAGE,
    )


@mcp.tool()
def get_weather(latitude: float, longitude: float) -> WeatherResult:
    """Gets the current weather conditions, 2-day recent precipitation, and a 3-day forecast.

    On failure this tool RETURNS a graceful WeatherResult fallback with
    ``status="unavailable"`` — it never raises.

    Args:
        latitude: The latitude coordinate (e.g. 53.3498).
        longitude: The longitude coordinate (e.g. -6.2603).
    """
    try:
        data = _fetch_weather(latitude, longitude)
        if data is None:
            logger.error("Weather service unavailable; returning graceful fallback.")
            return _unavailable_result()

        # Parse current weather
        current_data = data.get("current", {})
        current = CurrentWeather(
            temp_c=current_data.get("temperature_2m", 0.0),
            humidity_pct=current_data.get("relative_humidity_2m", 0.0),
            wind_kmh=current_data.get("wind_speed_10m", 0.0),
            precip_mm=current_data.get("precipitation", 0.0)
        )

        # Parse daily weather
        daily_data = data.get("daily", {})
        times = daily_data.get("time", [])
        precip_sums = daily_data.get("precipitation_sum", [])
        temp_maxes = daily_data.get("temperature_2m_max", [])
        temp_mins = daily_data.get("temperature_2m_min", [])
        weather_codes = daily_data.get("weathercode", [])

        # Calculate recent precipitation (sum of the last 2 days before today)
        # past_days=2 means daily arrays start with 2 historical days: index 0 and 1.
        recent_precip_mm_2d = 0.0
        if len(precip_sums) >= 2:
            recent_precip_mm_2d = sum(precip_sums[:2])

        # Parse forecast (next 3 days starting from today: index 2, 3, 4)
        forecast = []
        # Loop over index 2, 3, 4
        for i in range(2, 5):
            if i < len(times):
                forecast.append(
                    WeatherForecastDay(
                        date=times[i],
                        temp_max_c=temp_maxes[i] if i < len(temp_maxes) else 0.0,
                        temp_min_c=temp_mins[i] if i < len(temp_mins) else 0.0,
                        precip_mm=precip_sums[i] if i < len(precip_sums) else 0.0,
                        weathercode=weather_codes[i] if i < len(weather_codes) else 0
                    )
                )

        return WeatherResult(
            current=current,
            recent_precip_mm_2d=recent_precip_mm_2d,
            forecast=forecast
        )
    except Exception as e:
        # Belt-and-braces: never let the tool raise. Any unexpected parsing or
        # runtime error degrades to the graceful fallback.
        logger.error(f"Error fetching weather data: {_redact_coords(str(e))}")
        return _unavailable_result()

if __name__ == "__main__":
    mcp.run()
