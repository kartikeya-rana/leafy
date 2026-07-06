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
Deterministic unit tests for app/shelter/rules.py — no LLM, no quota, no
network. Pure function logic only.
"""
import pytest

from app.shelter.rules import (
    assess_plant,
    categorize_weather,
    resolve_forecast_day_index,
)


# ---------------------------------------------------------------------------
# categorize_weather
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("code,expected", [
    (0, 0), (1, 0),                       # sunny
    (2, 1), (3, 1), (45, 1), (48, 1),     # cloudy / fog
    (51, 2), (55, 2), (61, 2), (65, 2), (67, 2), (80, 2), (81, 2), (82, 2),  # rainy
    (95, 3), (96, 3), (99, 3),            # thunderstorm
    (71, 4), (75, 4), (77, 4), (85, 4), (86, 4),  # snow
])
def test_categorize_weather(code, expected):
    assert categorize_weather(code) == expected


@pytest.mark.parametrize("code", [-1, 4, 49, 68, 79, 87, 100, 200])
def test_categorize_weather_unrecognized_code_raises(code):
    with pytest.raises(ValueError):
        categorize_weather(code)


# ---------------------------------------------------------------------------
# assess_plant
# ---------------------------------------------------------------------------
def test_outdoor_plant_moved_indoors_when_category_exceeds_tolerance():
    # Basil-like tolerance: max_category=1, min_safe_temp_c=10
    tolerance = {"max_category": 1, "min_safe_temp_c": 10}
    result = assess_plant(day_category=2, day_temp_min=15, tolerance=tolerance, placement="outdoor")
    assert result["action"] == "move_indoors"
    assert "rainy" in result["reason"]


def test_outdoor_plant_moved_indoors_when_temp_exceeds_tolerance():
    tolerance = {"max_category": 3, "min_safe_temp_c": 10}
    result = assess_plant(day_category=0, day_temp_min=5, tolerance=tolerance, placement="outdoor")
    assert result["action"] == "move_indoors"
    assert "5" in result["reason"]


def test_outdoor_plant_kept_as_is_when_within_tolerance():
    # English Ivy-like tolerance: max_category=3, min_safe_temp_c=-5
    tolerance = {"max_category": 3, "min_safe_temp_c": -5}
    result = assess_plant(day_category=2, day_temp_min=8, tolerance=tolerance, placement="outdoor")
    assert result["action"] == "keep_as_is"


def test_indoor_plant_can_move_outdoors_when_mild():
    tolerance = {"max_category": 1, "min_safe_temp_c": 10}
    result = assess_plant(day_category=0, day_temp_min=18, tolerance=tolerance, placement="indoor")
    assert result["action"] == "move_outdoors"


def test_indoor_plant_stays_indoors_when_harsh():
    tolerance = {"max_category": 1, "min_safe_temp_c": 10}
    result = assess_plant(day_category=3, day_temp_min=18, tolerance=tolerance, placement="indoor")
    assert result["action"] == "keep_as_is"


def test_boundary_values_are_not_exceeded():
    # Exactly at the tolerance limits should NOT count as exceeding.
    tolerance = {"max_category": 2, "min_safe_temp_c": 5}
    outdoor_result = assess_plant(day_category=2, day_temp_min=5, tolerance=tolerance, placement="outdoor")
    assert outdoor_result["action"] == "keep_as_is"

    indoor_result = assess_plant(day_category=2, day_temp_min=5, tolerance=tolerance, placement="indoor")
    assert indoor_result["action"] == "move_outdoors"


def test_assess_plant_invalid_placement_raises():
    tolerance = {"max_category": 1, "min_safe_temp_c": 10}
    with pytest.raises(ValueError):
        assess_plant(day_category=0, day_temp_min=15, tolerance=tolerance, placement="greenhouse")


def test_assess_plant_reason_is_nonempty_string():
    tolerance = {"max_category": 1, "min_safe_temp_c": 10}
    result = assess_plant(day_category=0, day_temp_min=15, tolerance=tolerance, placement="outdoor")
    assert isinstance(result["reason"], str) and result["reason"]


# ---------------------------------------------------------------------------
# resolve_forecast_day_index
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text,expected", [
    ("today", 0),
    ("Today", 0),
    ("right now", 0),
    ("tomorrow", 1),
    ("Tomorrow", 1),
    ("what about tomorrow?", 1),
    ("day after tomorrow", 2),
    ("the day after tomorrow", 2),
    ("day after", 2),
])
def test_resolve_forecast_day_index(text, expected):
    assert resolve_forecast_day_index(text) == expected


def test_resolve_forecast_day_index_unrecognized_raises():
    with pytest.raises(ValueError):
        resolve_forecast_day_index("next Tuesday")


def test_resolve_forecast_day_index_empty_raises():
    with pytest.raises(ValueError):
        resolve_forecast_day_index("")
