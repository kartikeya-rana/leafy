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

from datetime import datetime, timezone, timedelta
from app.watering.rules import compute_watering_window

def test_overdue_watering():
    now = datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc)
    # last watered 10 days ago, interval is 5
    last_watered = now - timedelta(days=10)
    res = compute_watering_window(
        baseline_interval_days=5,
        min_days=2,
        max_days=10,
        last_watered_date=last_watered,
        now=now
    )
    assert res["status"] == "due"
    assert res["days_until_due"] == 0
    assert "today" in res["next_watering_window"]

def test_recent_watering_ok():
    now = datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc)
    # last watered 1 day ago, interval is 7
    last_watered = now - timedelta(days=1)
    res = compute_watering_window(
        baseline_interval_days=7,
        min_days=3,
        max_days=15,
        last_watered_date=last_watered,
        now=now
    )
    assert res["status"] == "ok"
    # remaining days: 7 - 1 = 6 days
    assert res["days_until_due"] == 6
    assert "in 5-6 days" in res["next_watering_window"] or "in 6-7 days" in res["next_watering_window"]

def test_recent_watering_soon():
    now = datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc)
    # last watered 4.5 days ago, interval is 5. 0.5 days left (<= 20% of 5)
    last_watered = now - timedelta(days=4.5)
    res = compute_watering_window(
        baseline_interval_days=5,
        min_days=2,
        max_days=10,
        last_watered_date=last_watered,
        now=now
    )
    assert res["status"] == "soon"
    assert res["days_until_due"] == 1

def test_weather_adjustment_hot():
    now = datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc)
    last_watered = now - timedelta(days=1)
    # baseline is 10 days. temp > 25 -> interval * 0.8 = 8 days.
    res = compute_watering_window(
        baseline_interval_days=10,
        min_days=2,
        max_days=15,
        last_watered_date=last_watered,
        weather={"current": {"temp_c": 30.0}},
        now=now
    )
    assert res["adjusted_interval"] == 8.0

def test_weather_adjustment_cold():
    now = datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc)
    last_watered = now - timedelta(days=1)
    # baseline is 5 days. temp < 15 -> interval * 1.2 = 6 days.
    res = compute_watering_window(
        baseline_interval_days=5,
        min_days=2,
        max_days=10,
        last_watered_date=last_watered,
        weather={"current": {"temp_c": 10.0}},
        now=now
    )
    assert res["adjusted_interval"] == 6.0

def test_weather_adjustment_rain():
    now = datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc)
    last_watered = now - timedelta(days=1)
    # baseline is 5 days. precip > 2.0 -> interval * 1.3 = 6.5 days.
    res = compute_watering_window(
        baseline_interval_days=5,
        min_days=2,
        max_days=10,
        last_watered_date=last_watered,
        weather={"current": {"precip_mm": 5.0}},
        now=now
    )
    assert res["adjusted_interval"] == 6.5

def test_clamping_behavior():
    now = datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc)
    last_watered = now - timedelta(days=1)
    # baseline is 20 days. max_days is 15. -> interval clamped to 15.
    res = compute_watering_window(
        baseline_interval_days=20,
        min_days=5,
        max_days=15,
        last_watered_date=last_watered,
        now=now
    )
    assert res["adjusted_interval"] == 15.0


def test_next_date_is_last_watered_plus_interval():
    # The single anchor: next_date == last_watered + adjusted_interval, and it
    # is independent of `now` (only status/days_until depend on the clock).
    last_watered = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
    res = compute_watering_window(
        baseline_interval_days=6,
        min_days=2,
        max_days=15,
        last_watered_date=last_watered,
        now=datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc),
    )
    assert res["adjusted_interval"] == 6.0
    assert res["next_date"] == last_watered + timedelta(days=6.0)


def test_outdoor_rain_extends_vs_dry():
    now = datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc)
    last_watered = now - timedelta(days=1)
    dry = compute_watering_window(
        baseline_interval_days=5, min_days=2, max_days=15,
        last_watered_date=last_watered, now=now, placement="outdoor",
        weather={"current": {"temp_c": 20.0, "precip_mm": 0.0}, "recent_precip_mm_2d": 0.0},
    )
    rained = compute_watering_window(
        baseline_interval_days=5, min_days=2, max_days=15,
        last_watered_date=last_watered, now=now, placement="outdoor",
        weather={"current": {"temp_c": 20.0, "precip_mm": 0.0}, "recent_precip_mm_2d": 8.0},
    )
    # Recent rain (> 2mm) extends the outdoor interval by 30%.
    assert dry["adjusted_interval"] == 5.0
    assert rained["adjusted_interval"] == 6.5
    assert rained["next_date"] > dry["next_date"]


def test_indoor_ignores_rain():
    now = datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc)
    last_watered = now - timedelta(days=1)
    dry = compute_watering_window(
        baseline_interval_days=5, min_days=2, max_days=15,
        last_watered_date=last_watered, now=now, placement="indoor",
        weather={"current": {"temp_c": 20.0, "precip_mm": 0.0}, "recent_precip_mm_2d": 0.0},
    )
    rained = compute_watering_window(
        baseline_interval_days=5, min_days=2, max_days=15,
        last_watered_date=last_watered, now=now, placement="indoor",
        weather={"current": {"temp_c": 20.0, "precip_mm": 0.0}, "recent_precip_mm_2d": 8.0},
    )
    # Indoor soil is never rained on: rain must not change the interval.
    assert dry["adjusted_interval"] == 5.0
    assert rained["adjusted_interval"] == 5.0
    assert rained["next_date"] == dry["next_date"]


def test_indoor_temperature_is_steadier_than_outdoor():
    now = datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc)
    last_watered = now - timedelta(days=1)
    hot = {"current": {"temp_c": 30.0, "precip_mm": 0.0}, "recent_precip_mm_2d": 0.0}
    outdoor = compute_watering_window(
        baseline_interval_days=10, min_days=2, max_days=15,
        last_watered_date=last_watered, now=now, placement="outdoor", weather=hot,
    )
    indoor = compute_watering_window(
        baseline_interval_days=10, min_days=2, max_days=15,
        last_watered_date=last_watered, now=now, placement="indoor", weather=hot,
    )
    # Warm now shortens both, but indoor reacts more gently (0.9 vs 0.8).
    assert outdoor["adjusted_interval"] == 8.0
    assert indoor["adjusted_interval"] == 9.0


def test_forecast_rain_extends_outdoor():
    now = datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc)
    last_watered = now - timedelta(days=1)
    # No recent rain, but the forecast (chat get_weather shape) shows rain today.
    res = compute_watering_window(
        baseline_interval_days=5, min_days=2, max_days=15,
        last_watered_date=last_watered, now=now, placement="outdoor",
        weather={
            "current": {"temp_c": 20.0, "precip_mm": 0.0},
            "recent_precip_mm_2d": 0.0,
            "forecast": [{"precip_mm": 9.0}],
        },
    )
    assert res["adjusted_interval"] == 6.5
