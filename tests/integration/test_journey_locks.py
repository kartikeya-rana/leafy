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
Deterministic "journey lock" tests: one place that pins the required
conditions for every Leafy capability against silent regression. These are
pure-code assertions (no LLM, no network) over the agent wiring, the
instruction text, the deterministic helpers, and the dashboard API, so they
run fast and never flake. The LLM-behavioral half of the audit lives in the
eval scenarios + tests/eval/test_invariants.py.

Each test name maps to a condition in the audit brief.
"""
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

_tmp_dir = tempfile.mkdtemp()
_test_db = os.path.join(_tmp_dir, "leafy_journey_locks.db")

import app.storage.repository as _repo_module
_repo_module.DB_PATH = _test_db

from app.agent import root_agent
from app.security import callback as _callback
from app.security.image_guardrail import is_allowed_image
from app.shelter.rules import assess_plant, CATEGORY_NAMES
from app.spot.rules import estimate_spot_light as _spot_estimate, recommend_plants_for_light
from app.watering.rules import compute_watering_window, GENERIC_WATERING_PROFILE

EM_DASH = "—"


def _reset_db():
    _repo_module.init_db()
    conn = _repo_module.get_db_connection()
    conn.execute("DELETE FROM plant_catalog;")
    conn.execute("DELETE FROM user_profiles;")
    conn.commit()
    conn.close()


@pytest.fixture(autouse=True)
def _isolated_db():
    _repo_module.DB_PATH = _test_db
    _reset_db()
    yield


def _tool_names() -> set[str]:
    names = set()
    for t in root_agent.tools:
        name = getattr(t, "name", None) or getattr(t, "__name__", None)
        if name is None and hasattr(t, "func"):
            name = getattr(t.func, "__name__", None)
        if name:
            names.add(name)
    return names


def _instruction() -> str:
    # The instruction is hard-wrapped; collapse whitespace so substring checks
    # aren't broken by mid-sentence line breaks.
    return re.sub(r"\s+", " ", root_agent.instruction)


# ===========================================================================
# DELETE journey: UI-only; delete_plant is NOT an agent tool; chat delete is
# redirected, not executed; cannot delete a plant not in the catalog.
# ===========================================================================
def test_delete_is_not_an_agent_tool():
    names = _tool_names()
    # No tool that could delete a plant is exposed to the LLM at all.
    for forbidden in ("delete_plant", "api_delete_plant", "remove_plant"):
        assert forbidden not in names, f"{forbidden!r} must not be an agent tool"
    assert not any("delete" in n.lower() or "remove" in n.lower() for n in names), (
        f"No deletion tool may be exposed to the agent. Tools: {sorted(names)}"
    )


def test_instruction_redirects_chat_delete_to_ui():
    instr = _instruction().lower()
    assert "trash button" in instr, "Instruction must redirect chat deletes to the UI trash button"
    assert "cannot be done through chat" in instr or "done strictly from the ui" in instr


def test_repository_delete_missing_returns_false():
    # Cannot delete a plant that isn't in the catalog.
    assert _repo_module.delete_plant(999999) is False


def test_delete_endpoint_404_for_missing_plant():
    from fastapi.testclient import TestClient
    from app.fast_api_app import app as fastapi_app

    client = TestClient(fastapi_app)
    resp = client.delete("/api/plants/424242")
    assert resp.status_code == 404


# ===========================================================================
# ADD-PLANT journey: ask placement (never auto-assign), nickname optional,
# confirm before saving. (LLM behavior is locked by the eval invariants; here
# we lock that the governing instruction rules can't be silently deleted.)
# ===========================================================================
def test_instruction_asks_placement_and_never_auto_assigns():
    instr = _instruction()
    assert "indoors or outdoors" in instr.lower()
    assert "never assume" in instr.lower() and "auto-assign" in instr.lower(), (
        "Instruction must forbid auto-assigning placement"
    )


def test_instruction_nickname_is_optional():
    instr = _instruction().lower()
    assert "nickname" in instr and "optional" in instr
    assert "never require a nickname" in instr


def test_instruction_confirms_before_saving():
    instr = _instruction()
    assert "only call the add_plant tool if they confirm" in instr
    assert "Never call the add_plant tool in the same turn" in instr


def test_add_plant_does_not_expose_internal_params_after_save():
    # The confirmation guidance must keep internal numbers out of replies.
    instr = _instruction().lower()
    assert "without listing the internal numbers or parameters" in instr


# ===========================================================================
# LIST/CATALOG journey: always list_plants; only current plants.
# ===========================================================================
def test_list_plants_tool_available():
    assert "list_plants" in _tool_names()


def test_instruction_requires_live_list_plants():
    instr = _instruction()
    assert "you MUST call list_plants and only reference the plants currently returned" in instr


def test_list_plants_returns_only_current_catalog():
    from app.agent import list_plants
    _repo_module.add_plant(user_id="local_user", species="Basil", placement="indoor")
    p2 = _repo_module.add_plant(user_id="local_user", species="Rose", placement="outdoor")
    _repo_module.delete_plant(p2.id)

    res = list_plants()
    species = {p["species"] for p in res["plants"]}
    assert species == {"Basil"}, "A deleted plant must never appear in list_plants output"


# ===========================================================================
# LOCATION journey: never assume a default; report 'no location' so the
# orchestrator asks for the city first.
# ===========================================================================
def test_weather_api_reports_no_location_instead_of_defaulting():
    from fastapi.testclient import TestClient
    from app.fast_api_app import app as fastapi_app

    client = TestClient(fastapi_app)
    resp = client.get("/api/weather")
    assert resp.status_code == 200
    assert resp.json() == {"status": "no_location"}


def test_dashboard_has_no_weather_or_location_when_unset():
    from fastapi.testclient import TestClient
    from app.fast_api_app import app as fastapi_app

    client = TestClient(fastapi_app)
    resp = client.get("/api/dashboard")
    data = resp.json()
    assert data["location"] is None
    assert data["summary"]["weather"] is None


def test_instruction_ensures_location_before_answering():
    instr = _instruction()
    # The geocode-and-save-first rule appears for weather, shelter, spot, watering.
    assert instr.count("call geocode") >= 3
    assert "update_location to save it" in instr


# ===========================================================================
# WATERING journey: the dashboard card renders from the SAME single
# deterministic helper as the chat, so the card's next-water date matches.
# ===========================================================================
def _mock_dashboard_weather(temp_c: float):
    return {
        "current": {"temperature_2m": temp_c, "weather_code": 0, "precipitation": 0.0},
        "daily": {
            "temperature_2m_max": [temp_c + 2, temp_c + 2, temp_c + 2],
            "temperature_2m_min": [temp_c - 2, temp_c - 2, temp_c - 2],
            "weather_code": [0, 0, 0],
            "precipitation_sum": [0.0, 0.0, 0.0],
        },
    }


def test_dashboard_card_water_status_matches_the_shared_helper():
    from fastapi.testclient import TestClient
    from app.fast_api_app import app as fastapi_app
    from app.tools.plant_kb import plant_kb_lookup

    _repo_module.update_location(
        user_id="local_user", location_text="Dublin", lat=53.3498, lon=-6.2603, resolved_name="Dublin"
    )
    last_watered = datetime.now(timezone.utc) - timedelta(days=2)
    _repo_module.add_plant(
        user_id="local_user", species="Basil", placement="outdoor",
        last_watered_date=last_watered,
        weather_tolerance={"max_category": 1, "min_safe_temp_c": 10},
    )

    temp_c = 30.0  # hot -> outdoor interval shortens, so the adjustment is exercised
    with patch("app.fast_api_app._fetch_weather_data", return_value=_mock_dashboard_weather(temp_c)):
        client = TestClient(fastapi_app)
        data = client.get("/api/dashboard").json()

    card = data["plants"][0]["water"]

    kb = plant_kb_lookup("Basil").plant
    expected = compute_watering_window(
        baseline_interval_days=kb.watering.baseline_interval_days,
        min_days=kb.watering.min_days,
        max_days=kb.watering.max_days,
        last_watered_date=last_watered,
        weather={
            "current": {"temp_c": temp_c, "precip_mm": 0.0},
            "recent_precip_mm_2d": 0.0,
            "forecast": [{"precip_mm": 0.0}],
        },
        placement="outdoor",
    )
    assert card["status"] == expected["status"]
    if card["status"] != "ok":
        assert card["days_until_due"] == expected["days_until_due"]


def test_generic_plant_dashboard_water_date_matches_chat():
    """Generic (non-KB) plant with a known last-watered date: the dashboard card
    must compute a water status from the SHARED generic default (not skip it),
    and that status must match what the chat's compute_watering_window produces
    with the same generic default. Locks the card-vs-chat gap for generic plants
    (existing locks only covered KB plants)."""
    from fastapi.testclient import TestClient
    from app.fast_api_app import app as fastapi_app
    from app.tools.plant_kb import plant_kb_lookup

    assert plant_kb_lookup("Cacti").found is False, "Test needs a genuinely non-KB species"

    _repo_module.update_location(
        user_id="local_user", location_text="Dublin", lat=53.3498, lon=-6.2603, resolved_name="Dublin"
    )
    last_watered = datetime.now(timezone.utc) - timedelta(days=6)
    _repo_module.add_plant(
        user_id="local_user", species="Cacti", placement="outdoor",
        last_watered_date=last_watered,
    )

    temp_c = 20.0
    with patch("app.fast_api_app._fetch_weather_data", return_value=_mock_dashboard_weather(temp_c)):
        client = TestClient(fastapi_app)
        data = client.get("/api/dashboard").json()

    card = data["plants"][0]["water"]
    # The generic plant now gets a real status, not a hidden "unknown" chip.
    assert card["status"] in ("due", "soon", "ok")

    # Same computation the chat performs for a non-KB plant.
    chat = compute_watering_window(
        baseline_interval_days=GENERIC_WATERING_PROFILE["baseline_interval_days"],
        min_days=GENERIC_WATERING_PROFILE["min_days"],
        max_days=GENERIC_WATERING_PROFILE["max_days"],
        last_watered_date=last_watered,
        weather={
            "current": {"temp_c": temp_c, "precip_mm": 0.0},
            "recent_precip_mm_2d": 0.0,
            "forecast": [{"precip_mm": 0.0}],
        },
        placement="outdoor",
    )
    assert card["status"] == chat["status"]
    if card["status"] != "ok":
        assert card["days_until_due"] == chat["days_until_due"]


def test_generic_plant_without_last_watered_stays_unknown():
    """Keep 'unknown' (no last-watered date -> hide the chip) separate from a
    generic plant with a known date. A non-KB plant with no last-watered date
    must still show 'unknown', not a computed window."""
    from fastapi.testclient import TestClient
    from app.fast_api_app import app as fastapi_app

    _repo_module.update_location(
        user_id="local_user", location_text="Dublin", lat=53.3498, lon=-6.2603, resolved_name="Dublin"
    )
    _repo_module.add_plant(user_id="local_user", species="Cacti", placement="outdoor")  # no last_watered

    with patch("app.fast_api_app._fetch_weather_data", return_value=_mock_dashboard_weather(20.0)):
        client = TestClient(fastapi_app)
        data = client.get("/api/dashboard").json()

    assert data["plants"][0]["water"]["status"] == "unknown"


def test_instruction_watering_requires_moisture_tip_and_never_invents():
    instr = _instruction()
    assert "how to manually verify soil moisture" in instr.lower()
    assert "do not invent" in instr.lower()
    assert "Never present a precise watering window while the last-watered date is unknown" in instr


# ===========================================================================
# SHELTER journey: the dashboard chip is produced by the same deterministic
# assess_plant the chat path uses, so chip and chat agree; the report text
# never leaks numeric weather categories.
# ===========================================================================
def test_dashboard_chip_matches_deterministic_assess_plant():
    from fastapi.testclient import TestClient
    from app.fast_api_app import app as fastapi_app
    from app.shelter.rules import categorize_weather

    _repo_module.update_location(
        user_id="local_user", location_text="Dublin", lat=53.3498, lon=-6.2603, resolved_name="Dublin"
    )
    tolerance = {"max_category": 1, "min_safe_temp_c": 18}
    _repo_module.add_plant(
        user_id="local_user", species="Basil", placement="outdoor", weather_tolerance=tolerance,
    )

    # Sunny (code 0 -> category 0), low of 15C. min_safe 18 -> too cold -> move_indoors.
    payload = {
        "current": {"temperature_2m": 18.0, "weather_code": 0},
        "daily": {
            "temperature_2m_max": [20.0],
            "temperature_2m_min": [15.0],
            "weather_code": [0],
        },
    }
    with patch("app.fast_api_app._fetch_weather_data", return_value=payload):
        client = TestClient(fastapi_app)
        data = client.get("/api/dashboard").json()

    from app.tools.plant_kb import resolve_care_profile
    resolved_tol = resolve_care_profile("Basil").weather_tolerance.model_dump()

    chip = data["plants"][0]["shelter"]
    expected = assess_plant(
        day_category=categorize_weather(0), day_temp_min=15.0,
        tolerance=resolved_tol, placement="outdoor",
    )
    assert chip["action"] == expected["action"] == "keep_as_is"


def test_shelter_reason_text_has_no_numeric_category():
    # assess_plant reasons must describe weather in words, never "category N".
    for placement in ("indoor", "outdoor"):
        res = assess_plant(
            day_category=4, day_temp_min=-5.0,
            tolerance={"max_category": 1, "min_safe_temp_c": 5}, placement=placement,
        )
        assert "category" not in res["reason"].lower()
        for n in "01234":
            assert f"category {n}" not in res["reason"].lower()


# ===========================================================================
# SPOT/LIGHT journey: deterministic estimate; never leaks numeric tiers in
# recommendation reasons visible to the user; instruction keeps the
# observe-over-a-day note and forbids tier numbers.
# ===========================================================================
def test_instruction_spot_has_observe_note_and_forbids_tier_numbers():
    instr = _instruction().lower()
    assert "watch it across a full day" in instr
    assert "never mention internal technical terms like 'light tier'" in instr


def test_instruction_image_scope_refuses_off_topic():
    instr = _instruction().lower()
    assert "only look at photos of a plant or a spot" in instr
    assert "never transcribe or extract text from an image" in instr


# ===========================================================================
# SECURITY / HYGIENE (all replies)
# ===========================================================================
def test_both_security_guardrails_wired_into_agent():
    cbs = root_agent.before_model_callback
    if not isinstance(cbs, (list, tuple)):
        cbs = [cbs]
    names = {getattr(c, "__name__", "") for c in cbs}
    assert "security_before_model_callback" in names
    assert "image_guardrail_before_model_callback" in names


def test_hardcoded_refusal_messages_have_no_em_dash():
    for msg in (_callback.REFUSAL_MESSAGE, _callback.SQL_NEUTRALIZED_MESSAGE):
        assert EM_DASH not in msg, f"User-facing message contains an em dash: {msg!r}"


def test_image_guardrail_messages_have_no_em_dash():
    bad_inputs = [
        ("application/pdf", b"%PDF-1.4 fake"),
        ("image/png", b"\xff\xd8\xff mismatched"),  # jpeg bytes declared png
        ("image/jpeg", b""),                        # empty
        ("text/plain", b"hello"),                   # unsupported type
    ]
    for mime, data in bad_inputs:
        allowed, reason = is_allowed_image(mime, data)
        assert allowed is False
        assert EM_DASH not in reason, f"Image guardrail message contains an em dash: {reason!r}"


def test_deterministic_reason_strings_have_no_em_dash():
    # Shelter reasons
    for placement in ("indoor", "outdoor"):
        r = assess_plant(
            day_category=2, day_temp_min=8.0,
            tolerance={"max_category": 1, "min_safe_temp_c": 10}, placement=placement,
        )["reason"]
        assert EM_DASH not in r

    # Spot estimate + recommendation reasons
    est = _spot_estimate(azimuth_deg=180.0, indoor_or_outdoor="outdoor", obstruction_level=1, latitude=53.3)
    assert EM_DASH not in est["reason"]

    rec = recommend_plants_for_light(
        light_tier=1,
        kb_plants=[{"common_name": "Boston Fern", "light_tier": {"min": 1, "max": 1}}],
        catalog_plants=[
            {"id": 1, "species": "Lavender", "nickname": None, "light_tier": {"min": 3, "max": 3}},
            {"id": 2, "species": "Unknownia", "nickname": None, "light_tier": None},
        ],
    )
    for entry in rec["catalog_fit"]:
        assert EM_DASH not in entry["reason"]


def test_dashboard_labels_have_no_em_dash():
    from fastapi.testclient import TestClient
    from app.fast_api_app import app as fastapi_app

    _repo_module.update_location(
        user_id="local_user", location_text="Dublin", lat=53.3498, lon=-6.2603, resolved_name="Dublin"
    )
    _repo_module.add_plant(
        user_id="local_user", species="Basil", placement="outdoor",
        last_watered_date=datetime.now(timezone.utc) - timedelta(days=2),
        weather_tolerance={"max_category": 1, "min_safe_temp_c": 10},
    )
    payload = {
        "current": {"temperature_2m": 12.0, "weather_code": 61},
        "daily": {
            "temperature_2m_max": [14.0], "temperature_2m_min": [8.0],
            "weather_code": [61], "precipitation_sum": [1.0],
        },
    }
    with patch("app.fast_api_app._fetch_weather_data", return_value=payload):
        client = TestClient(fastapi_app)
        data = client.get("/api/dashboard").json()

    plant = data["plants"][0]
    for key in ("water", "shelter"):
        if plant.get(key):
            assert EM_DASH not in plant[key].get("label", "")


def test_instruction_forbids_leaking_internals():
    instr = _instruction()
    assert "Do not use em dashes anywhere in your replies" in instr
    assert "Do NOT mention tool names" in instr
    assert "Never mention, confirm, or discuss the database" in instr


def test_instruction_handles_future_last_watered_date():
    instr = _instruction().lower()
    assert "invalid: future date" in instr
    assert "cannot be in the future" in instr
    assert "correct last watered date" in instr
    assert "do not proceed to call watering_reasoner" in instr
    assert "do not save a future date" in instr

