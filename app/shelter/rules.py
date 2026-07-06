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
Pure, dependency-free decision logic for the Shelter Advisor capability.

No ADK / google-genai imports here on purpose — deterministic, unit-testable
without any LLM or network call. The ADK graph that calls into this module
lives in app/shelter/graph.py.
"""

from typing import TypedDict

# ---------------------------------------------------------------------------
# Weather categorization (WMO weather code -> 0..4)
# ---------------------------------------------------------------------------
CATEGORY_NAMES: dict[int, str] = {
    0: "sunny",
    1: "cloudy",
    2: "rainy",
    3: "thunderstorm",
    4: "snow",
}


def categorize_weather(weathercode: int) -> int:
    """Maps a WMO daily weather code to a 0..4 severity category.

    Scale: 0 sunny, 1 cloudy, 2 rainy, 3 thunderstorm, 4 snow.

    Args:
        weathercode: The WMO weather code (e.g. from Open-Meteo's daily
            'weathercode' field), such as 0 (clear sky) or 61 (moderate rain).

    Returns:
        An int 0..4.

    Raises:
        ValueError: If the code isn't a recognized WMO code.
    """
    if weathercode in (0, 1):
        return 0
    if weathercode in (2, 3, 45, 48):
        return 1
    if weathercode in range(51, 68) or weathercode in (80, 81, 82):
        return 2
    if weathercode in (95, 96, 97, 98, 99):
        return 3
    if weathercode in range(71, 78) or weathercode in (85, 86):
        return 4
    raise ValueError(f"Unrecognized WMO weathercode: {weathercode!r}")


# ---------------------------------------------------------------------------
# Per-plant shelter assessment
# ---------------------------------------------------------------------------
class ShelterAssessment(TypedDict):
    action: str  # 'move_indoors' | 'move_outdoors' | 'keep_as_is'
    reason: str


def _describe_conditions(day_category: int, day_temp_min: float, tolerance: dict) -> str:
    day_name = CATEGORY_NAMES[day_category]
    max_category = tolerance["max_category"]
    min_safe_temp_c = tolerance["min_safe_temp_c"]
    max_category_name = CATEGORY_NAMES[max_category]

    if min_safe_temp_c < 0:
        temp_word = "freezing conditions"
    elif min_safe_temp_c <= 10:
        temp_word = "cool temperatures"
    elif min_safe_temp_c <= 15:
        temp_word = "mild temperatures"
    else:
        temp_word = "warm temperatures"

    return (
        f"forecast is {day_name} with a low of {day_temp_min:g}°C; "
        f"tolerance is up to {max_category_name} and down to {temp_word}"
    )


def assess_plant(
    day_category: int,
    day_temp_min: float,
    tolerance: dict,
    placement: str,
) -> ShelterAssessment:
    """Decides whether a plant should move indoors/outdoors for the day.

    Deterministic: the plant needs shelter if the day's weather category
    exceeds its stored max_category, or the day's low temperature is below
    its stored min_safe_temp_c.

    Args:
        day_category: The day's weather category, 0..4 (see categorize_weather).
        day_temp_min: The day's forecast low temperature in Celsius.
        tolerance: dict with 'max_category' (int, 0..4) and 'min_safe_temp_c' (number).
        placement: The plant's current placement, 'indoor' or 'outdoor'.

    Returns:
        {'action': 'move_indoors' | 'move_outdoors' | 'keep_as_is', 'reason': str}

    Raises:
        ValueError: If placement isn't 'indoor' or 'outdoor'.
    """
    if placement not in ("indoor", "outdoor"):
        raise ValueError(f"placement must be 'indoor' or 'outdoor', got {placement!r}")

    exceeds_category = day_category > tolerance["max_category"]
    exceeds_cold = day_temp_min < tolerance["min_safe_temp_c"]
    exceeds = exceeds_category or exceeds_cold
    conditions = _describe_conditions(day_category, day_temp_min, tolerance)

    if placement == "outdoor":
        if exceeds:
            return {
                "action": "move_indoors",
                "reason": f"Bring it indoors: {conditions}, which is harsher than it can safely handle outside.",
            }
        return {
            "action": "keep_as_is",
            "reason": f"Safe to leave outdoors: {conditions}, within its tolerance.",
        }

    # placement == "indoor"
    if not exceeds:
        return {
            "action": "move_outdoors",
            "reason": f"You could bring it outside today: {conditions}, well within its tolerance.",
        }
    return {
        "action": "keep_as_is",
        "reason": f"Keep it indoors: {conditions}, which exceeds what it could safely tolerate outside.",
    }


# ---------------------------------------------------------------------------
# Day selection (today / tomorrow / day after -> forecast index)
# ---------------------------------------------------------------------------
# Checked in order — "day after tomorrow" must be matched before the plain
# "tomorrow" keyword, since it contains "tomorrow" as a substring.
_DAY_KEYWORDS: list[tuple[tuple[str, ...], int]] = [
    (("today", "right now", "this evening", "now"), 0),
    (("day after tomorrow", "day after", "in 2 days", "2 days from now"), 2),
    (("tomorrow", "next day", "in 1 day"), 1),
]


def resolve_forecast_day_index(day_text: str) -> int:
    """Resolves free-form day text ('today' / 'tomorrow' / 'day after
    tomorrow') to a forecast list index (0, 1, or 2 respectively) — matching
    the get_weather forecast ordering (today, tomorrow, day after tomorrow).

    Args:
        day_text: Free-form day reference, e.g. 'today', 'tomorrow',
            'the day after tomorrow'.

    Returns:
        0 for today, 1 for tomorrow, 2 for the day after tomorrow.

    Raises:
        ValueError: If day_text doesn't match a recognized day reference.
    """
    t = (day_text or "").strip().lower()
    for keywords, index in _DAY_KEYWORDS:
        if any(keyword in t for keyword in keywords):
            return index
    raise ValueError(
        f"Could not resolve a forecast day from {day_text!r}. "
        "Expected something like 'today', 'tomorrow', or 'day after tomorrow'."
    )
