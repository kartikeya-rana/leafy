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
Integration test for the Shelter Advisor ADK graph (app/shelter/graph.py):
fetch_forecast -> categorize -> assess_plants -> report.

No LLM anywhere in this graph, so the only thing mocked is the weather HTTP
call (via mcp_servers.weather_server.get_weather) — the graph wiring and the
deterministic rules underneath both run for real.
"""
import os
import tempfile
from unittest.mock import patch

import pytest

# Redirect the repository to a throwaway DB *before* importing from app.
_tmp_dir = tempfile.mkdtemp()
_test_db = os.path.join(_tmp_dir, "leafy_shelter_test.db")

import app.storage.repository as _repo_module
_repo_module.DB_PATH = _test_db

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from mcp_servers.weather_server import CurrentWeather, WeatherForecastDay, WeatherResult


def _make_weather_mock() -> WeatherResult:
    return WeatherResult(
        current=CurrentWeather(temp_c=5.0, humidity_pct=90.0, wind_kmh=30.0, precip_mm=5.0),
        recent_precip_mm_2d=10.0,
        forecast=[
            # today: thunderstorm (95), cold — should exceed most tolerances
            WeatherForecastDay(date="2026-07-04", temp_max_c=8.0, temp_min_c=2.0, precip_mm=20.0, weathercode=95),
            # tomorrow: sunny, mild
            WeatherForecastDay(date="2026-07-05", temp_max_c=20.0, temp_min_c=15.0, precip_mm=0.0, weathercode=1),
            # day after: light rain
            WeatherForecastDay(date="2026-07-06", temp_max_c=16.0, temp_min_c=9.0, precip_mm=3.0, weathercode=61),
        ],
    )


@pytest.mark.asyncio
async def test_shelter_advisor_today_recommends_moving_tender_plants_indoors():
    _repo_module.init_db()
    _repo_module.get_or_create_profile("local_user")
    _repo_module.update_location(
        user_id="local_user", location_text="Dublin, Ireland", lat=53.3498, lon=-6.2603, resolved_name="Dublin"
    )

    # Basil-like tolerance, outdoors — should need to come in during a storm.
    _repo_module.add_plant(
        user_id="local_user", species="Basil", placement="outdoor",
        weather_tolerance={"max_category": 1, "min_safe_temp_c": 10},
    )
    # English-Ivy-like tolerance, outdoors — hardy enough to stay put.
    _repo_module.add_plant(
        user_id="local_user", species="English Ivy", placement="outdoor",
        weather_tolerance={"max_category": 3, "min_safe_temp_c": -5},
    )
    # A tender plant currently indoors — should stay indoors during a storm.
    _repo_module.add_plant(
        user_id="local_user", species="Pothos", placement="indoor",
        weather_tolerance={"max_category": 1, "min_safe_temp_c": 12},
    )

    from app.shelter.graph import shelter_advisor

    session_svc = InMemorySessionService()
    runner = Runner(app_name="shelter_test", agent=shelter_advisor, session_service=session_svc)
    session = await session_svc.create_session(app_name="shelter_test", user_id="u1")

    with patch("mcp_servers.weather_server.get_weather", return_value=_make_weather_mock()):
        events = [
            ev
            async for ev in runner.run_async(
                user_id="u1",
                session_id=session.id,
                new_message=types.Content(role="user", parts=[types.Part(text="today")]),
            )
        ]

    texts = [p.text for ev in events if ev.content for p in (ev.content.parts or []) if p.text]
    report_text = "\n".join(texts)

    assert "thunderstorm" in report_text.lower()
    assert "Basil" in report_text and "Move indoors" in report_text
    assert "English Ivy" in report_text and "Keep as is" in report_text
    assert "Pothos" in report_text and "Keep as is" in report_text
    assert "verify" in report_text.lower()


@pytest.mark.asyncio
async def test_shelter_advisor_tomorrow_recommends_moving_indoor_plant_outdoors():
    _repo_module.init_db()
    _repo_module.get_or_create_profile("local_user")
    _repo_module.update_location(
        user_id="local_user", location_text="Dublin, Ireland", lat=53.3498, lon=-6.2603, resolved_name="Dublin"
    )
    _repo_module.add_plant(
        user_id="local_user", species="Pothos", placement="indoor",
        weather_tolerance={"max_category": 1, "min_safe_temp_c": 12},
    )

    from app.shelter.graph import shelter_advisor

    session_svc = InMemorySessionService()
    runner = Runner(app_name="shelter_test2", agent=shelter_advisor, session_service=session_svc)
    session = await session_svc.create_session(app_name="shelter_test2", user_id="u1")

    with patch("mcp_servers.weather_server.get_weather", return_value=_make_weather_mock()):
        events = [
            ev
            async for ev in runner.run_async(
                user_id="u1",
                session_id=session.id,
                new_message=types.Content(role="user", parts=[types.Part(text="tomorrow")]),
            )
        ]

    texts = [p.text for ev in events if ev.content for p in (ev.content.parts or []) if p.text]
    report_text = "\n".join(texts)

    assert "sunny" in report_text.lower()
    assert "Pothos" in report_text and "Could move outdoors" in report_text


@pytest.mark.asyncio
async def test_shelter_advisor_scoped_to_single_plant():
    _repo_module.init_db()
    _repo_module.get_or_create_profile("local_user")
    _repo_module.update_location(
        user_id="local_user", location_text="Dublin, Ireland", lat=53.3498, lon=-6.2603, resolved_name="Dublin"
    )

    # Add three different plants
    _repo_module.add_plant(
        user_id="local_user", species="Basil", placement="outdoor", nickname="My Basil",
        weather_tolerance={"max_category": 1, "min_safe_temp_c": 10},
    )
    _repo_module.add_plant(
        user_id="local_user", species="English Ivy", placement="outdoor", nickname="Ivy",
        weather_tolerance={"max_category": 3, "min_safe_temp_c": -5},
    )

    from app.shelter.graph import shelter_advisor

    session_svc = InMemorySessionService()
    runner = Runner(app_name="shelter_test_scoped", agent=shelter_advisor, session_service=session_svc)
    session = await session_svc.create_session(app_name="shelter_test_scoped", user_id="u1")

    # Ask for Basil only
    with patch("mcp_servers.weather_server.get_weather", return_value=_make_weather_mock()):
        events = [
            ev
            async for ev in runner.run_async(
                user_id="u1",
                session_id=session.id,
                new_message=types.Content(role="user", parts=[types.Part(text="today|My Basil")]),
            )
        ]

    texts = [p.text for ev in events if ev.content for p in (ev.content.parts or []) if p.text]
    report_text = "\n".join(texts)

    assert "Basil" in report_text
    assert "Ivy" not in report_text
    assert "English Ivy" not in report_text

    # Ask for a non-existent plant
    session2 = await session_svc.create_session(app_name="shelter_test_scoped", user_id="u1")
    with patch("mcp_servers.weather_server.get_weather", return_value=_make_weather_mock()):
        events2 = [
            ev
            async for ev in runner.run_async(
                user_id="u1",
                session_id=session2.id,
                new_message=types.Content(role="user", parts=[types.Part(text="today|Nonexistent")]),
            )
        ]

    texts2 = [p.text for ev in events2 if ev.content for p in (ev.content.parts or []) if p.text]
    report_text2 = "\n".join(texts2)
    assert 'No plants matching "Nonexistent" were found' in report_text2
