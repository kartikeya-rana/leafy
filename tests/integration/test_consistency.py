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
from app.shelter.rules import assess_plant
from app.spot.rules import estimate_spot_light, recommend_plants_for_light

def test_watering_consistency():
    # Verify that calling compute_watering_window directly produces the expected status and range
    now = datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc)
    last_watered = now - timedelta(days=2)
    res = compute_watering_window(
        baseline_interval_days=5,
        min_days=2,
        max_days=10,
        last_watered_date=last_watered,
        now=now
    )
    assert res["status"] == "ok"
    assert "in 3-4 days" in res["next_watering_window"]


def _dashboard_call(baseline, min_d, max_d, last_watered, now, placement, temp, cur_precip, recent_precip, today_precip):
    """Reproduce how app.fast_api_app builds its weather_arg for the card."""
    weather_arg = {
        "current": {"temp_c": temp, "precip_mm": cur_precip},
        "recent_precip_mm_2d": recent_precip,
        "forecast": [{"precip_mm": today_precip}],
    }
    return compute_watering_window(
        baseline_interval_days=baseline, min_days=min_d, max_days=max_d,
        last_watered_date=last_watered, now=now, placement=placement, weather=weather_arg,
    )


def _chat_call(baseline, min_d, max_d, last_watered, now, placement, temp, cur_precip, recent_precip, today_precip):
    """Reproduce how app.agent's watering_reasoner builds its weather (the full
    get_weather result shape) for the chat answer."""
    weather = {
        "current": {
            "temp_c": temp, "humidity_pct": 50.0, "wind_kmh": 5.0, "precip_mm": cur_precip,
        },
        "recent_precip_mm_2d": recent_precip,
        "forecast": [
            {"date": "2026-07-05", "temp_max_c": temp + 3, "temp_min_c": temp - 3,
             "precip_mm": today_precip, "weathercode": 61},
        ],
    }
    return compute_watering_window(
        baseline_interval_days=baseline, min_days=min_d, max_days=max_d,
        last_watered_date=last_watered, now=now, placement=placement, weather=weather,
    )


def test_dashboard_and_chat_next_date_match():
    """The card and the chat must agree on next_date exactly, across indoor vs
    outdoor and rained-recently vs dry, since both derive it from the single
    compute_watering_window anchor with the same inputs."""
    now = datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc)
    last_watered = now - timedelta(days=1)

    scenarios = [
        # (placement, temp, cur_precip, recent_precip, today_precip)
        ("outdoor", 20.0, 0.0, 0.0, 0.0),    # dry
        ("outdoor", 20.0, 0.0, 8.0, 0.0),    # rained recently
        ("outdoor", 30.0, 0.0, 0.0, 0.0),    # hot & dry -> shorten
        ("outdoor", 20.0, 0.0, 0.0, 9.0),    # rain in forecast
        ("indoor", 20.0, 0.0, 8.0, 9.0),     # indoor ignores all rain
        ("indoor", 30.0, 0.0, 0.0, 0.0),     # indoor warm (steadier)
    ]

    for placement, temp, cur_precip, recent_precip, today_precip in scenarios:
        dash = _dashboard_call(5, 2, 10, last_watered, now, placement, temp, cur_precip, recent_precip, today_precip)
        chat = _chat_call(5, 2, 10, last_watered, now, placement, temp, cur_precip, recent_precip, today_precip)
        assert dash["next_date"] == chat["next_date"], f"next_date mismatch for {placement}, temp={temp}, recent={recent_precip}"
        assert dash["adjusted_interval"] == chat["adjusted_interval"]
        assert dash["status"] == chat["status"]
        # And the human-readable window strings agree too.
        assert dash["next_watering_window"] == chat["next_watering_window"]


def test_indoor_vs_outdoor_diverge_when_rained():
    """Sanity check the fix is meaningful: with recent rain, an outdoor plant's
    next_date is pushed later than an identical indoor plant's."""
    now = datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc)
    last_watered = now - timedelta(days=1)
    outdoor = _dashboard_call(5, 2, 10, last_watered, now, "outdoor", 20.0, 0.0, 8.0, 0.0)
    indoor = _dashboard_call(5, 2, 10, last_watered, now, "indoor", 20.0, 0.0, 8.0, 0.0)
    assert outdoor["next_date"] > indoor["next_date"]

def test_shelter_consistency():
    # Verify that assess_plant behaves consistently with the categories used by the server
    tolerance = {"max_category": 1, "min_safe_temp_c": 10}
    
    # Mild day: category 1 (cloudy), 15°C
    assessment = assess_plant(
        day_category=1,
        day_temp_min=15.0,
        tolerance=tolerance,
        placement="outdoor"
    )
    assert assessment["action"] == "keep_as_is"

    # Mild day for indoor plant: category 1, 19°C -> can move outdoors
    assessment = assess_plant(
        day_category=1,
        day_temp_min=19.0,
        tolerance=tolerance,
        placement="indoor"
    )
    assert assessment["action"] == "move_outdoors"

    # Cold day: low of 5°C -> move indoors
    assessment = assess_plant(
        day_category=1,
        day_temp_min=5.0,
        tolerance=tolerance,
        placement="outdoor"
    )
    assert assessment["action"] == "move_indoors"

def test_spot_light_consistency():
    # Verify that estimate_spot_light classifies correctly
    res = estimate_spot_light(
        azimuth_deg=180.0,
        indoor_or_outdoor="outdoor",
        obstruction_level=0,
        latitude=53.3498
    )
    assert res["light_tier"] == 3  # bright direct light
