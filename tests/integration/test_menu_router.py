"""
Integration tests for the Leafy main-menu router.

Drives the graph end-to-end via the ADK Runner with an in-memory session
service and a throwaway SQLite DB (isolated per test via a fixture), so
these tests are independent of test_watering_flow.py and of each other.

Scripted routes:
  "add a plant"     -> add_plant_node (species -> save) -> loops to main_menu
  "list my plants"  -> list_plants_node (reads catalog)  -> loops to main_menu
  "watering advice" -> the existing 4-node pipeline (unchanged), reached via
                       a routed edge from main_menu instead of unconditionally
                       from START.
"""
import json
import os
import tempfile
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# SET ASIDE: app.agent no longer defines a main_menu router node or a graph
# Workflow at all — it's now a single LLM-orchestrated root agent (see
# app/agent.py's module docstring). These scripted-route tests drive a
# node/RequestInput mechanism that no longer exists. Orchestrator behavior
# (including "add a plant" / "list my plants" / "watering advice" style
# requests, now handled in free-form conversation) is validated via
# LLM-as-judge evaluation instead. Left in place for reference.
# ---------------------------------------------------------------------------
pytestmark = pytest.mark.skip(
    reason="Graph Workflow replaced by an LLM-orchestrated agent; see test plan in the eval phase."
)

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

import app.storage.repository as _repo_module

from app.tools.geocode import GeocodeResult
from mcp_servers.weather_server import WeatherResult, CurrentWeather, WeatherForecastDay
from google.adk.models.llm_response import LlmResponse


# ---------------------------------------------------------------------------
# Isolation: give every test its own throwaway DB file.
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch):
    tmp_dir = tempfile.mkdtemp()
    db_path = os.path.join(tmp_dir, "leafy_menu_test.db")
    monkeypatch.setattr(_repo_module, "DB_PATH", db_path)
    _repo_module.init_db()
    yield


# ---------------------------------------------------------------------------
# Helpers (mirrors tests/integration/test_watering_flow.py)
# ---------------------------------------------------------------------------
def _make_resume_message(interrupt_id: str, answer: str) -> types.Content:
    """Build a FunctionResponse Content that the ADK Runner treats as a resume."""
    return types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id=interrupt_id,
                    name="adk_request_input",
                    response={"result": answer},
                )
            )
        ],
    )


def _collect_interrupt_ids(events: list) -> list[str]:
    """Extract RequestInput interrupt IDs (function_call ids) from events, in order."""
    ids = []
    for ev in events:
        if ev.content:
            for part in (ev.content.parts or []):
                if (
                    part.function_call
                    and part.function_call.name == "adk_request_input"
                    and part.function_call.id
                ):
                    ids.append(part.function_call.id)
    return ids


def _collect_texts(events: list) -> list[str]:
    """Extract plain text parts from events, in order."""
    texts = []
    for ev in events:
        if ev.content:
            for part in (ev.content.parts or []):
                if part.text:
                    texts.append(part.text)
    return texts


def _make_geo_mock() -> GeocodeResult:
    return GeocodeResult(
        found=True,
        lat=53.3498,
        lon=-6.2603,
        resolved_name="Dublin",
        country="Ireland",
        message="OK",
    )


def _make_weather_mock() -> WeatherResult:
    return WeatherResult(
        current=CurrentWeather(
            temp_c=15.0, humidity_pct=70.0, wind_kmh=12.0, precip_mm=0.0
        ),
        recent_precip_mm_2d=1.5,
        forecast=[
            WeatherForecastDay(date="2026-07-04", temp_max_c=17.0, temp_min_c=11.0, precip_mm=0.0),
            WeatherForecastDay(date="2026-07-05", temp_max_c=18.0, temp_min_c=12.0, precip_mm=0.2),
            WeatherForecastDay(date="2026-07-06", temp_max_c=16.0, temp_min_c=10.0, precip_mm=1.0),
        ],
    )


_REASONER_JSON = json.dumps({
    "next_watering_window": "in 2-3 days, by July 6th",
    "reason": "Soil is still moist from recent watering; mild weather ahead.",
    "moisture_check": "Insert finger 2 cm into soil — water when dry.",
    "is_generic_guidance": True,
})


def _make_llm_response() -> LlmResponse:
    return LlmResponse(
        content=types.Content(
            role="model",
            parts=[types.Part(text=_REASONER_JSON)],
        ),
        partial=False,
        turn_complete=True,
    )


async def _fake_llm(*args, **kwargs):
    yield _make_llm_response()


def _make_runner():
    from app.agent import root_agent

    session_svc = InMemorySessionService()
    runner = Runner(
        app_name="leafy_menu_test",
        agent=root_agent,
        session_service=session_svc,
    )
    return runner, session_svc


# ---------------------------------------------------------------------------
# Test 1 — "add a plant" route
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_menu_add_a_plant_saves_plant_and_returns_to_menu():
    runner, session_svc = _make_runner()
    session = await session_svc.create_session(app_name="leafy_menu_test", user_id="u1")

    async def run_turn(content):
        return [ev async for ev in runner.run_async(user_id="u1", session_id=session.id, new_message=content)]

    # Turn 1: kick off -> expect the main menu prompt.
    t1 = await run_turn(types.Content(role="user", parts=[types.Part(text="hi")]))
    ids1 = _collect_interrupt_ids(t1)
    assert ids1 == ["menu_choice"], f"Expected main menu prompt, got {ids1}"

    # Turn 2: choose "add a plant" -> expect species prompt.
    t2 = await run_turn(_make_resume_message(ids1[0], "add a plant"))
    ids2 = _collect_interrupt_ids(t2)
    assert ids2 == ["add_plant_species"], f"Expected species prompt, got {ids2}"

    # Turn 3: provide species -> plant saved, loops back to menu.
    t3 = await run_turn(_make_resume_message(ids2[0], "Basil"))
    ids3 = _collect_interrupt_ids(t3)
    texts3 = _collect_texts(t3)

    plants = _repo_module.list_plants("local_user")
    assert plants, "Plant catalog is empty — Basil was never saved"
    assert any("basil" in p.species.lower() for p in plants), (
        f"Basil not in catalog. Found: {[p.species for p in plants]}"
    )
    assert any("Added" in t and "Basil" in t for t in texts3), (
        f"Expected an 'Added Basil' confirmation message, got: {texts3}"
    )
    assert ids3 == ["menu_choice"], f"Expected the router to loop back to the main menu, got {ids3}"


# ---------------------------------------------------------------------------
# Test 2 — "list my plants" route
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_menu_list_my_plants_shows_catalog_and_returns_to_menu():
    # Pre-seed the catalog directly (isolate this test from the add-plant flow).
    _repo_module.add_plant(user_id="local_user", species="Sweet Basil", placement="indoor")
    _repo_module.add_plant(user_id="local_user", species="Snake Plant", placement="indoor")

    runner, session_svc = _make_runner()
    session = await session_svc.create_session(app_name="leafy_menu_test", user_id="u1")

    async def run_turn(content):
        return [ev async for ev in runner.run_async(user_id="u1", session_id=session.id, new_message=content)]

    t1 = await run_turn(types.Content(role="user", parts=[types.Part(text="hi")]))
    ids1 = _collect_interrupt_ids(t1)
    assert ids1 == ["menu_choice"], f"Expected main menu prompt, got {ids1}"

    t2 = await run_turn(_make_resume_message(ids1[0], "list my plants"))
    ids2 = _collect_interrupt_ids(t2)
    texts2 = _collect_texts(t2)

    combined = "\n".join(texts2)
    assert "Sweet Basil" in combined, f"Expected catalog listing to mention 'Sweet Basil', got: {texts2}"
    assert "Snake Plant" in combined, f"Expected catalog listing to mention 'Snake Plant', got: {texts2}"
    assert ids2 == ["menu_choice"], f"Expected the router to loop back to the main menu, got {ids2}"


# ---------------------------------------------------------------------------
# Test 3 — "watering advice" route (existing pipeline, reached via the router)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_menu_watering_advice_produces_recommendation():
    runner, session_svc = _make_runner()
    session = await session_svc.create_session(app_name="leafy_menu_test", user_id="u1")

    async def run_turn(content):
        return [ev async for ev in runner.run_async(user_id="u1", session_id=session.id, new_message=content)]

    with (
        patch("app.agent.geocode", return_value=_make_geo_mock()),
        patch("mcp_servers.weather_server.get_weather", return_value=_make_weather_mock()),
        patch(
            "google.adk.models.google_llm.Gemini.generate_content_async",
            side_effect=_fake_llm,
        ),
    ):
        t1 = await run_turn(types.Content(role="user", parts=[types.Part(text="hi")]))
        ids1 = _collect_interrupt_ids(t1)
        assert ids1 == ["menu_choice"], f"Expected main menu prompt, got {ids1}"

        t2 = await run_turn(_make_resume_message(ids1[0], "watering advice"))
        ids2 = _collect_interrupt_ids(t2)
        assert ids2 == ["location_input"], f"Expected location prompt, got {ids2}"

        t3 = await run_turn(_make_resume_message(ids2[0], "Dublin, Ireland"))
        ids3 = _collect_interrupt_ids(t3)
        assert ids3 == ["add_plant_input"], f"Expected species prompt (empty catalog), got {ids3}"

        t4 = await run_turn(_make_resume_message(ids3[0], "Basil"))
        ids4 = _collect_interrupt_ids(t4)
        assert ids4 == ["last_watered_input"], f"Expected last-watered prompt, got {ids4}"

        t5 = await run_turn(_make_resume_message(ids4[0], "yesterday"))
        ids5 = _collect_interrupt_ids(t5)
        assert ids5 == ["placement_input"], f"Expected placement prompt, got {ids5}"

        t6 = await run_turn(_make_resume_message(ids5[0], "indoor"))
        ids6 = _collect_interrupt_ids(t6)
        texts6 = _collect_texts(t6)

    assert not ids6, f"Expected the pipeline to complete with no further prompts, got {ids6}"
    combined = "\n".join(texts6)
    assert "next_watering_window" in combined or "watering" in combined.lower(), (
        f"Expected a watering recommendation in the final turn's output, got: {texts6}"
    )

    plants = _repo_module.list_plants("local_user")
    assert plants and any("basil" in p.species.lower() for p in plants)
    profile = _repo_module.get_or_create_profile("local_user")
    assert profile.lat is not None and profile.lon is not None
