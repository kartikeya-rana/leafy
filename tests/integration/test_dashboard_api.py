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
Regression test for GET /api/dashboard's shelter aggregation: the shelter
action must be one of move_indoors / move_outdoors / keep_as_is (not just
a two-way move_indoors-vs-everything-else split), and move_count must count
BOTH move actions so the summary number matches the number of "to move"
chips actually rendered on the cards. No LLM calls; the weather fetch is
mocked so day_category/day_temp_min are deterministic.
"""
import os
import tempfile
from unittest.mock import patch

import pytest

_tmp_dir = tempfile.mkdtemp()
_test_db = os.path.join(_tmp_dir, "leafy_dashboard_test.db")

import app.storage.repository as _repo_module
_repo_module.DB_PATH = _test_db


def _reset_db():
    _repo_module.init_db()
    conn = _repo_module.get_db_connection()
    conn.execute("DELETE FROM plant_catalog;")
    conn.execute("DELETE FROM user_profiles;")
    conn.commit()
    conn.close()


def _mock_weather_payload():
    """A mild, sunny day: category 0, low of 8C -- mild enough that an
    indoor plant with a modest tolerance (Boston Fern, min 7C) could go outside,
    and cold enough for Basil (min 10C) to move indoors."""
    return {
        "current": {"temperature_2m": 18.0, "weather_code": 0},
        "daily": {
            "temperature_2m_max": [20.0],
            "temperature_2m_min": [8.0],
            "weather_code": [0],
        },
    }


@pytest.fixture(autouse=True)
def _isolated_db():
    _reset_db()
    yield


def test_dashboard_shelter_action_move_outdoors_is_reported_and_counted():
    _repo_module.update_location(
        user_id="local_user", location_text="Dublin, Ireland", lat=53.3498, lon=-6.2603, resolved_name="Dublin"
    )
    # Indoor plant, mild tolerance -- a sunny mild day is within its
    # tolerance, so it should be offered a trip outside (move_outdoors).
    _repo_module.add_plant(
        user_id="local_user", species="Boston Fern", placement="indoor",
        weather_tolerance={"max_category": 1, "min_safe_temp_c": 7},
    )

    with patch("app.fast_api_app._fetch_weather_data", return_value=_mock_weather_payload()):
        from fastapi.testclient import TestClient
        from app.fast_api_app import app as fastapi_app

        client = TestClient(fastapi_app)
        resp = client.get("/api/dashboard")

    assert resp.status_code == 200
    data = resp.json()

    plant = data["plants"][0]
    assert plant["shelter"]["action"] == "move_outdoors"
    assert plant["shelter"]["label"] == "Could go outside"

    # The summary count must include this move_outdoors plant, not just
    # move_indoors ones, so the number matches the single "to move" chip
    # actually rendered on the card.
    assert data["summary"]["move_count"] == 1


def test_dashboard_shelter_move_indoors_and_move_outdoors_both_counted():
    _repo_module.update_location(
        user_id="local_user", location_text="Dublin, Ireland", lat=53.3498, lon=-6.2603, resolved_name="Dublin"
    )
    # Outdoor plant with a low tolerance for a 15C low -- should be told to
    # come in (move_indoors).
    _repo_module.add_plant(
        user_id="local_user", species="Basil", placement="outdoor",
        weather_tolerance={"max_category": 1, "min_safe_temp_c": 18},
    )
    # Indoor plant well within tolerance for a mild sunny day -- should be
    # offered a trip outside (move_outdoors).
    _repo_module.add_plant(
        user_id="local_user", species="Boston Fern", placement="indoor",
        weather_tolerance={"max_category": 1, "min_safe_temp_c": 7},
    )
    # Outdoor plant that's hardy enough to just stay put (keep_as_is).
    _repo_module.add_plant(
        user_id="local_user", species="English Ivy", placement="outdoor",
        weather_tolerance={"max_category": 3, "min_safe_temp_c": -5},
    )

    with patch("app.fast_api_app._fetch_weather_data", return_value=_mock_weather_payload()):
        from fastapi.testclient import TestClient
        from app.fast_api_app import app as fastapi_app

        client = TestClient(fastapi_app)
        resp = client.get("/api/dashboard")

    data = resp.json()
    by_species = {p["species"]: p["shelter"] for p in data["plants"]}

    assert by_species["Basil"]["action"] == "move_indoors"
    assert by_species["Basil"]["label"] == "Bring indoors"
    assert by_species["Boston Fern"]["action"] == "move_outdoors"
    assert by_species["Boston Fern"]["label"] == "Could go outside"
    assert by_species["English Ivy"]["action"] == "keep_as_is"
    assert by_species["English Ivy"]["label"] == "Fine where it is"

    # Both move_indoors and move_outdoors count toward "to move" -- the
    # summary number must match the two move-labeled cards, not just one.
    assert data["summary"]["move_count"] == 2
