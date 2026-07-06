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

"""
Pure, dependency-free decision logic for the Watering Advisor capability.

Deterministic: calculates next-watering window and due status clamped to min/max
days, adjusting based on placement and weather conditions.

Single source of truth: `compute_watering_window` computes ONE anchor date,
``next_date = last_watered + adjusted_interval`` (clamped to [min, max]), and
derives EVERY output field — status, days_until_due, and the human-readable
window string — from that same date. The dashboard card and the chat reasoner
both call this function, so as long as they pass the same inputs they render
identical, mutually consistent answers.
"""

import math
from datetime import datetime, timedelta, timezone

# --- Adjustment thresholds ---------------------------------------------------
WARM_C = 25.0          # at/above this "now" temperature soil dries faster
COOL_C = 15.0          # at/below this "now" temperature soil dries slower
WET_MM = 2.0           # rain (recent or forecast) above this counts as "wet"

# Outdoor soil swings with the weather; indoor soil is steadier (it is never
# rained on and dries more slowly), so it gets a gentler temperature response.
OUTDOOR_WARM_FACTOR = 0.8
OUTDOOR_COOL_FACTOR = 1.2
OUTDOOR_WET_FACTOR = 1.3
INDOOR_WARM_FACTOR = 0.9
INDOOR_COOL_FACTOR = 1.1

# Shared fallback watering profile for a plant that is NOT in the knowledge
# base. Both the dashboard card and the chat watering flow use this same
# default so a generic (non-KB) plant with a known last-watered date gets an
# identical deterministic window on both surfaces (instead of the card showing
# nothing while the chat free-reasons a different answer).
GENERIC_WATERING_PROFILE = {
    "baseline_interval_days": 7,
    "min_days": 5,
    "max_days": 10,
}


def format_date_with_ordinal(dt: datetime) -> str:
    day = dt.day
    if 11 <= day <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return dt.strftime(f"%B {day}{suffix}")


def _as_dict(obj) -> dict:
    """Coerce a pydantic model / arbitrary object / dict into a plain dict."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    return {}


def _extract_weather_signals(weather) -> tuple[float, float, float]:
    """Pull (current_temp_c, current_precip_mm, wet_signal_mm) out of a weather
    payload. Accepts both the dashboard's minimal dict and the chat's full
    get_weather result (dict or pydantic). ``wet_signal_mm`` is the strongest of
    recent 2-day precipitation and the next forecast day's precipitation, so
    "rain in the last 2 days (or forecast)" is captured in a single number."""
    weather = _as_dict(weather)
    if not weather:
        return 20.0, 0.0, 0.0

    current = _as_dict(weather.get("current"))
    temp = current.get("temp_c", current.get("temperature_2m", 20.0))
    precip_now = current.get("precip_mm", current.get("precipitation", 0.0))

    recent_precip = weather.get("recent_precip_mm_2d", 0.0) or 0.0

    # Look at the nearest forecast day, if the payload carries a forecast list.
    forecast_precip = 0.0
    forecast = weather.get("forecast") or []
    if forecast:
        first = _as_dict(forecast[0])
        forecast_precip = first.get("precip_mm", first.get("precipitation", 0.0)) or 0.0

    wet_signal = max(float(recent_precip), float(forecast_precip))
    return float(temp), float(precip_now), wet_signal


def compute_watering_window(
    baseline_interval_days: int,
    min_days: int,
    max_days: int,
    last_watered_date: datetime,
    weather: dict | None = None,
    now: datetime | None = None,
    placement: str = "outdoor",
) -> dict:
    """Deterministic watering window helper — the single source of truth shared
    by the dashboard card and the chat reasoner.

    Interval adjustments (all clamped to [min_days, max_days]):
      - Warm now (temp >= 25°C)  -> shorten (soil dries faster)
      - Cool now (temp <= 15°C)  -> extend  (soil dries slower)
      - Outdoor + rain in the last 2 days or forecast (> 2mm) -> extend
        (soil is already wet)
      - Indoor -> ignore rain entirely and use a gentler temperature response
        (indoor soil is never rained on and dries more slowly, so its interval
        is steadier)

    Every returned field is derived from a single anchor date::

        next_date  = last_watered_date + adjusted_interval   (clamped)
        days_until = next_date - now

    Returns:
        {
            'status': 'due' | 'soon' | 'ok',   # due if days_until <= 0,
                                               # soon if <= 2, else ok
            'days_until_due': int,
            'next_watering_window': window_str, # built from next_date
            'adjusted_interval': float,
            'next_date': datetime,              # the shared anchor
        }
    """
    if now is None:
        now = datetime.now(timezone.utc)

    is_outdoor = str(placement).lower() != "indoor"
    adjusted_interval = float(baseline_interval_days)

    if weather:
        temp, precip_now, wet_signal = _extract_weather_signals(weather)

        # 1. Temperature: warm -> shorten, cool -> extend. Indoor soil is
        #    steadier, so it reacts to temperature more gently.
        if temp >= WARM_C:
            adjusted_interval *= OUTDOOR_WARM_FACTOR if is_outdoor else INDOOR_WARM_FACTOR
        elif temp <= COOL_C:
            adjusted_interval *= OUTDOOR_COOL_FACTOR if is_outdoor else INDOOR_COOL_FACTOR

        # 2. Rain: outdoors only. Indoor soil isn't rained on, so rain is ignored.
        if is_outdoor and (precip_now > WET_MM or wet_signal > WET_MM):
            adjusted_interval *= OUTDOOR_WET_FACTOR

    # Clamp interval to [min_days, max_days].
    adjusted_interval = max(float(min_days), min(float(max_days), adjusted_interval))

    # --- Single anchor: next_date. Everything below derives from it. ---------
    next_date = last_watered_date + timedelta(days=adjusted_interval)
    days_until = (next_date - now).total_seconds() / 86400.0

    if days_until <= 0:
        status = "due"
        days_until_due = 0
        window_str = f"today, {format_date_with_ordinal(now)}"
    else:
        status = "soon" if days_until <= 2.0 else "ok"
        days_until_due = max(1, math.ceil(days_until))

        # Human-readable range, anchored on the same next_date.
        low_days = max(1, math.floor(days_until))
        high_days = max(low_days + 1, math.ceil(days_until))
        if low_days == high_days:
            high_days = low_days + 1
        window_str = f"in {low_days}-{high_days} days, by {format_date_with_ordinal(next_date)}"

    return {
        "status": status,
        "days_until_due": days_until_due,
        "next_watering_window": window_str,
        "adjusted_interval": adjusted_interval,
        "next_date": next_date,
    }
