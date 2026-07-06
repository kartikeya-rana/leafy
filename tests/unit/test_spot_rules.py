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
Deterministic unit tests for app/spot/rules.py — no LLM, no quota, no
network, no database. Pure function logic only.
"""
import pytest

from app.spot.rules import (
    cardinal_to_azimuth,
    estimate_spot_light,
    recommend_plants_for_light,
)


# ---------------------------------------------------------------------------
# cardinal_to_azimuth
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text,expected", [
    ("N", 0.0), ("north", 0.0),
    ("NE", 45.0), ("northeast", 45.0), ("north-east", 45.0),
    ("E", 90.0), ("east", 90.0),
    ("SE", 135.0), ("southeast", 135.0),
    ("S", 180.0), ("south", 180.0),
    ("SW", 225.0), ("southwest", 225.0),
    ("W", 270.0), ("west", 270.0),
    ("NW", 315.0), ("northwest", 315.0),
])
def test_cardinal_to_azimuth_named_directions(text, expected):
    assert cardinal_to_azimuth(text) == expected


def test_cardinal_to_azimuth_is_case_insensitive_and_trims_whitespace():
    assert cardinal_to_azimuth("  South  ") == 180.0
    assert cardinal_to_azimuth("sW") == 225.0


@pytest.mark.parametrize("text,expected", [
    ("180", 180.0),
    ("202.5", 202.5),
    ("0", 0.0),
    ("400", 40.0),   # normalized modulo 360
])
def test_cardinal_to_azimuth_numeric_passthrough(text, expected):
    assert cardinal_to_azimuth(text) == expected


def test_cardinal_to_azimuth_unparseable_raises():
    with pytest.raises(ValueError):
        cardinal_to_azimuth("sideways")


def test_cardinal_to_azimuth_empty_raises():
    with pytest.raises(ValueError):
        cardinal_to_azimuth("")


# ---------------------------------------------------------------------------
# estimate_spot_light — aspect baselines (Dublin latitude, no obstruction, outdoor)
# ---------------------------------------------------------------------------
DUBLIN_LAT = 53.3498


def test_south_facing_outdoor_northern_hemisphere_is_tier_3():
    result = estimate_spot_light(azimuth_deg=180, indoor_or_outdoor="outdoor", obstruction_level=0, latitude=DUBLIN_LAT)
    assert result["light_tier"] == 3
    assert "south" in result["reason"].lower() or "S-facing" in result["reason"]


@pytest.mark.parametrize("azimuth", [90, 270])
def test_east_west_facing_outdoor_northern_hemisphere_is_tier_2(azimuth):
    result = estimate_spot_light(azimuth_deg=azimuth, indoor_or_outdoor="outdoor", obstruction_level=0, latitude=DUBLIN_LAT)
    assert result["light_tier"] == 2


def test_north_facing_outdoor_northern_hemisphere_is_tier_1():
    result = estimate_spot_light(azimuth_deg=0, indoor_or_outdoor="outdoor", obstruction_level=0, latitude=DUBLIN_LAT)
    assert result["light_tier"] == 1


# ---------------------------------------------------------------------------
# estimate_spot_light — southern hemisphere is mirrored
# ---------------------------------------------------------------------------
SYDNEY_LAT = -33.87


def test_north_facing_outdoor_southern_hemisphere_is_tier_3():
    result = estimate_spot_light(azimuth_deg=0, indoor_or_outdoor="outdoor", obstruction_level=0, latitude=SYDNEY_LAT)
    assert result["light_tier"] == 3


def test_south_facing_outdoor_southern_hemisphere_is_tier_1():
    result = estimate_spot_light(azimuth_deg=180, indoor_or_outdoor="outdoor", obstruction_level=0, latitude=SYDNEY_LAT)
    assert result["light_tier"] == 1


@pytest.mark.parametrize("azimuth", [90, 270])
def test_east_west_facing_outdoor_southern_hemisphere_is_tier_2(azimuth):
    result = estimate_spot_light(azimuth_deg=azimuth, indoor_or_outdoor="outdoor", obstruction_level=0, latitude=SYDNEY_LAT)
    assert result["light_tier"] == 2


# ---------------------------------------------------------------------------
# estimate_spot_light — high-latitude cap
# ---------------------------------------------------------------------------
def test_high_latitude_caps_south_facing_at_tier_2():
    # 60 N (e.g. Anchorage-ish) -- south-facing would otherwise be tier 3
    result = estimate_spot_light(azimuth_deg=180, indoor_or_outdoor="outdoor", obstruction_level=0, latitude=60.0)
    assert result["light_tier"] == 2


def test_very_high_latitude_caps_south_facing_at_tier_1():
    # 70 N (arctic) -- capped hard regardless of aspect
    result = estimate_spot_light(azimuth_deg=180, indoor_or_outdoor="outdoor", obstruction_level=0, latitude=70.0)
    assert result["light_tier"] == 1


def test_high_latitude_cap_is_symmetric_for_southern_hemisphere():
    result = estimate_spot_light(azimuth_deg=0, indoor_or_outdoor="outdoor", obstruction_level=0, latitude=-70.0)
    assert result["light_tier"] == 1


def test_moderate_latitude_below_cap_threshold_unaffected():
    # Dublin (~53N) is below the 55 cap threshold -- south stays tier 3
    result = estimate_spot_light(azimuth_deg=180, indoor_or_outdoor="outdoor", obstruction_level=0, latitude=DUBLIN_LAT)
    assert result["light_tier"] == 3


# ---------------------------------------------------------------------------
# estimate_spot_light — obstruction
# ---------------------------------------------------------------------------
def test_obstruction_level_1_subtracts_one_tier():
    result = estimate_spot_light(azimuth_deg=180, indoor_or_outdoor="outdoor", obstruction_level=1, latitude=DUBLIN_LAT)
    assert result["light_tier"] == 2


def test_obstruction_level_2_subtracts_two_tiers():
    result = estimate_spot_light(azimuth_deg=180, indoor_or_outdoor="outdoor", obstruction_level=2, latitude=DUBLIN_LAT)
    assert result["light_tier"] == 1


def test_obstruction_cannot_push_below_zero():
    result = estimate_spot_light(azimuth_deg=0, indoor_or_outdoor="outdoor", obstruction_level=2, latitude=DUBLIN_LAT)
    assert result["light_tier"] == 0


# ---------------------------------------------------------------------------
# estimate_spot_light — indoor is one tier lower than the same-facing outdoor spot
# ---------------------------------------------------------------------------
def test_indoor_is_one_tier_lower_than_same_facing_outdoor():
    outdoor = estimate_spot_light(azimuth_deg=180, indoor_or_outdoor="outdoor", obstruction_level=0, latitude=DUBLIN_LAT)
    indoor = estimate_spot_light(azimuth_deg=180, indoor_or_outdoor="indoor", obstruction_level=0, latitude=DUBLIN_LAT)
    assert indoor["light_tier"] == outdoor["light_tier"] - 1


def test_indoor_north_facing_clamps_at_zero_not_negative():
    result = estimate_spot_light(azimuth_deg=0, indoor_or_outdoor="indoor", obstruction_level=0, latitude=DUBLIN_LAT)
    assert result["light_tier"] == 0


# ---------------------------------------------------------------------------
# estimate_spot_light — validation and reason text
# ---------------------------------------------------------------------------
def test_invalid_indoor_or_outdoor_raises():
    with pytest.raises(ValueError):
        estimate_spot_light(azimuth_deg=180, indoor_or_outdoor="greenhouse", obstruction_level=0, latitude=DUBLIN_LAT)


def test_invalid_obstruction_level_raises():
    with pytest.raises(ValueError):
        estimate_spot_light(azimuth_deg=180, indoor_or_outdoor="outdoor", obstruction_level=5, latitude=DUBLIN_LAT)


def test_estimate_spot_light_reason_is_nonempty_string():
    result = estimate_spot_light(azimuth_deg=180, indoor_or_outdoor="outdoor", obstruction_level=0, latitude=DUBLIN_LAT)
    assert isinstance(result["reason"], str) and result["reason"]


def test_azimuth_wraps_around_360():
    # -10 degrees and 350 degrees are the same aspect (N)
    a = estimate_spot_light(azimuth_deg=-10, indoor_or_outdoor="outdoor", obstruction_level=0, latitude=DUBLIN_LAT)
    b = estimate_spot_light(azimuth_deg=350, indoor_or_outdoor="outdoor", obstruction_level=0, latitude=DUBLIN_LAT)
    assert a["light_tier"] == b["light_tier"] == 1


# ---------------------------------------------------------------------------
# recommend_plants_for_light
# ---------------------------------------------------------------------------
_KB_PLANTS = [
    {"common_name": "Snake Plant", "light_tier": {"min": 0, "max": 2}},
    {"common_name": "Lavender", "light_tier": {"min": 3, "max": 3}},
    {"common_name": "Boston Fern", "light_tier": {"min": 1, "max": 1}},
]


def test_recommend_plants_for_light_returns_only_fitting_kb_plants():
    result = recommend_plants_for_light(light_tier=3, kb_plants=_KB_PLANTS, catalog_plants=[])
    names = [p["common_name"] for p in result["recommended"]]
    assert names == ["Lavender"]


def test_recommend_plants_for_light_tier_within_range_boundaries():
    result = recommend_plants_for_light(light_tier=0, kb_plants=_KB_PLANTS, catalog_plants=[])
    names = {p["common_name"] for p in result["recommended"]}
    assert names == {"Snake Plant"}

    result = recommend_plants_for_light(light_tier=1, kb_plants=_KB_PLANTS, catalog_plants=[])
    names = {p["common_name"] for p in result["recommended"]}
    assert names == {"Snake Plant", "Boston Fern"}


def test_recommend_plants_for_light_catalog_fit_true_when_within_range():
    catalog = [{"id": 1, "species": "Snake Plant", "nickname": "Slyth", "light_tier": {"min": 0, "max": 2}}]
    result = recommend_plants_for_light(light_tier=2, kb_plants=_KB_PLANTS, catalog_plants=catalog)
    assert result["catalog_fit"][0]["fits"] is True


def test_recommend_plants_for_light_catalog_fit_false_when_too_little_light():
    catalog = [{"id": 2, "species": "Lavender", "nickname": None, "light_tier": {"min": 3, "max": 3}}]
    result = recommend_plants_for_light(light_tier=0, kb_plants=_KB_PLANTS, catalog_plants=catalog)
    entry = result["catalog_fit"][0]
    assert entry["fits"] is False
    assert "too little" in entry["reason"]


def test_recommend_plants_for_light_catalog_fit_false_when_too_much_light():
    catalog = [{"id": 3, "species": "Boston Fern", "nickname": None, "light_tier": {"min": 1, "max": 1}}]
    result = recommend_plants_for_light(light_tier=3, kb_plants=_KB_PLANTS, catalog_plants=catalog)
    entry = result["catalog_fit"][0]
    assert entry["fits"] is False
    assert "too much" in entry["reason"]


def test_recommend_plants_for_light_catalog_unknown_light_tier_is_none():
    catalog = [{"id": 4, "species": "Mystery Plant", "nickname": None, "light_tier": None}]
    result = recommend_plants_for_light(light_tier=2, kb_plants=_KB_PLANTS, catalog_plants=catalog)
    entry = result["catalog_fit"][0]
    assert entry["fits"] is None
    assert "no light data" in entry["reason"].lower()


def test_recommend_plants_for_light_preserves_catalog_order():
    catalog = [
        {"id": 1, "species": "Snake Plant", "nickname": None, "light_tier": {"min": 0, "max": 2}},
        {"id": 2, "species": "Lavender", "nickname": None, "light_tier": {"min": 3, "max": 3}},
    ]
    result = recommend_plants_for_light(light_tier=2, kb_plants=_KB_PLANTS, catalog_plants=catalog)
    assert [p["id"] for p in result["catalog_fit"]] == [1, 2]
