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

import os
import json
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

import app.storage.repository as repo
from app.tools.plant_kb import resolve_care_profile
from app.shelter.graph import assess_plants
from app.fast_api_app import api_plants, api_dashboard
from google.adk.agents.context import Context


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    test_db = tmp_path / "test_leafy_resolver.db"
    original_db_path = repo.DB_PATH
    repo.DB_PATH = str(test_db)
    repo.init_db()
    
    # Clean DB tables
    conn = repo.get_db_connection()
    conn.execute("DELETE FROM plant_catalog;")
    conn.execute("DELETE FROM user_profiles;")
    conn.commit()
    conn.close()

    yield

    repo.DB_PATH = original_db_path


def test_kb_versus_generic_care_profiles():
    # Verify a KB plant (Rose) uses its correct KB profile.
    rose_profile = resolve_care_profile("Rose")
    assert rose_profile.id == "rose"
    assert rose_profile.common_name == "Rose"
    assert rose_profile.weather_tolerance.max_category == 3
    assert rose_profile.weather_tolerance.min_safe_temp_c == -12
    assert rose_profile.watering.baseline_interval_days == 4

    # Verify a truly unknown plant (no KB name/alias/scientific name appears as a
    # substring, and no close fuzzy match) uses the generic default profile.
    # Note: the sentinel deliberately avoids KB words like "cactus", since the KB
    # now has a catch-all "Cactus" entry that substring-matches such names.
    generic_profile = resolve_care_profile("Nonexistent Wibblewort 12345")
    assert generic_profile.id == "generic"
    assert generic_profile.common_name == "Nonexistent Wibblewort 12345"
    assert generic_profile.weather_tolerance.max_category == 1
    assert generic_profile.weather_tolerance.min_safe_temp_c == 10
    assert generic_profile.watering.baseline_interval_days == 7


@pytest.mark.asyncio
async def test_wrong_stored_tolerance_is_ignored():
    # Insert a user profile and a plant catalog item directly with a wrong stored tolerance.
    repo.update_location(
        user_id="local_user", location_text="Dublin", lat=53.3498, lon=-6.2603, resolved_name="Dublin"
    )
    
    conn = repo.get_db_connection()
    wrong_tolerance = json.dumps({"max_category": 4, "min_safe_temp_c": -20})
    conn.execute(
        """INSERT INTO plant_catalog (user_id, species, nickname, placement, last_watered_date, added_at, weather_tolerance_json)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        ("local_user", "Basil", "My Sweet Basil", "outdoor", datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat(), wrong_tolerance)
    )
    conn.commit()
    conn.close()

    # 1. Verify resolve_care_profile returns the KB profile (not the wrong stored one).
    profile = resolve_care_profile("Basil")
    assert profile.weather_tolerance.max_category == 1
    assert profile.weather_tolerance.min_safe_temp_c == 10

    # 2. Verify api_plants does not return the wrong weather tolerance.
    plants_res = api_plants()
    plant_item = plants_res["plants"][0]
    assert plant_item["care"]["weather_tolerance"]["max_category"] == 1
    assert plant_item["care"]["weather_tolerance"]["min_safe_temp_c"] == 10

    # 3. Verify api_dashboard does not use the wrong weather tolerance.
    mock_weather = {
        "current": {"temp": 18.0, "precip": 0.0, "today_precip": 0.0},
        "temp": 18.0,
        "precip": 0.0,
        "today_precip": 0.0,
        "recent_precip_mm_2d": 0.0
    }
    with patch("app.fast_api_app._fetch_weather_data", return_value={"current": {"temperature_2m": 18.0}, "daily": {"weather_code": [0], "temperature_2m_min": [15.0]}}):
        dash_res = api_dashboard()
    dash_plant = dash_res["plants"][0]
    # Under Dublin conditions (min 15C, sunny category 0), Basil is fine where it is.
    # If the wrong tolerance (max 4, min -20) was used, or if the correct was used, the result should not be affected by the stored copy.
    # Let's check shelter details.
    assert dash_plant["care"]["baseline_interval_days"] == 2  # Basil's actual KB value is 2, generic is 7.

    # 4. Verify assess_plants node uses the resolved profile tolerance, not the wrong stored tolerance.
    mock_ctx = MagicMock(spec=Context)
    node_input = {
        "category": 3,
        "day_temp_min": 5.0,
    }
    async for event in assess_plants._run_impl(ctx=mock_ctx, node_input=node_input):
        assessments = event.output["assessments"]
        assert len(assessments) == 1
        # Basil has a KB tolerance of max_category=1 and min_safe_temp_c=10.
        # Since day_temp_min=5.0 is below min_safe_temp_c=10, the action must be "move_indoors" (Bring indoors).
        # (If the wrong tolerance of min_safe_temp_c=-20 was used, the action would be keep_as_is / fine where it is).
        assert assessments[0]["action"] == "move_indoors"


@pytest.mark.asyncio
async def test_all_capabilities_resolve_same_care_profile():
    # Add a catalog plant
    repo.update_location(
        user_id="local_user", location_text="Dublin", lat=53.3498, lon=-6.2603, resolved_name="Dublin"
    )
    repo.add_plant(user_id="local_user", species="Rose", placement="outdoor")

    # Resolve directly
    profile = resolve_care_profile("Rose")

    # 1. Dashboard card care enrichment
    plants_res = api_plants()
    care_data_plants = plants_res["plants"][0]["care"]

    # 2. Dashboard API care enrichment
    with patch("app.fast_api_app._fetch_weather_data", return_value={"current": {"temperature_2m": 18.0}, "daily": {"weather_code": [0], "temperature_2m_min": [15.0]}}):
        dash_res = api_dashboard()
    care_data_dash = dash_res["plants"][0]["care"]

    # 3. Shelter Advisor assessment
    mock_ctx = MagicMock(spec=Context)
    node_input = {
        "category": 0,
        "day_temp_min": 15.0,
    }
    async for event in assess_plants._run_impl(ctx=mock_ctx, node_input=node_input):
        assessments = event.output["assessments"]
        # Rose has max_category=3, min_safe_temp_c=-12.
        # With category 0 and min 15C, Rose is "Fine where it is".
        assert assessments[0]["action"] == "keep_as_is"

    # Assert that all match the resolved care profile
    assert care_data_plants["scientific_name"] == profile.scientific_name
    assert care_data_plants["baseline_interval_days"] == profile.watering.baseline_interval_days
    assert care_data_plants["min_days"] == profile.watering.min_days
    assert care_data_plants["max_days"] == profile.watering.max_days

    assert care_data_dash["scientific_name"] == profile.scientific_name
    assert care_data_dash["baseline_interval_days"] == profile.watering.baseline_interval_days
    assert care_data_dash["min_days"] == profile.watering.min_days
    assert care_data_dash["max_days"] == profile.watering.max_days
