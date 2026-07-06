"""
Integration test for the Leafy Watering Advisor workflow.

Drives the full 5-node graph end-to-end using the ADK Runner with an
in-memory session service and a throwaway SQLite DB, so it is:
  - isolated  (no shared state with the production DB)
  - repeatable (temp DB deleted after each run)
  - deterministic (geocode, weather, and LLM calls are all mocked)

Scripted human answers:
  menu_choice   -> "watering advice"  (routes past the main-menu router)
  location      -> "Dublin, Ireland"
  plant         -> "Basil"
  last_watered  -> "yesterday"
  placement     -> "indoor"

Assertions:
  1. The workflow reaches watering_reasoner (all 4 function nodes pass).
  2. No unhandled errors occur.
  3. The plant catalog in the test DB contains "Basil" (or "Sweet Basil").
  4. The user profile has lat/lon saved.

Empirical findings documented here:
  (a) On resume, ADK re-enters the *interrupted* node — it does NOT restart
      from START. Completed nodes are replayed from the event log without
      re-executing their generator body.
  (b) ctx.state does NOT persist across separate runner.run_async() turns
      when using InMemorySessionService (the in-memory store is not a
      persistent cross-turn store for session state via state_delta).
      ctx.resume_inputs contains ONLY the latest answer.
      => Thread ALL inter-node data via Event(output=...) / node_input.
"""
import json
import os
import tempfile
from typing import Any
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

# ---------------------------------------------------------------------------
# SET ASIDE: app.agent no longer defines a graph Workflow — it's now a single
# LLM-orchestrated root agent that calls tools in whatever order the model
# decides (see app/agent.py's module docstring). The scripted turn-by-turn
# HITL sequence this file drives (RequestInput / resume_inputs) no longer
# exists, so these tests can't run against the current agent. Orchestrator
# behavior is validated via LLM-as-judge evaluation instead (see the eval
# skill) rather than pinned tool-call order. Left in place for reference.
# ---------------------------------------------------------------------------
pytestmark = pytest.mark.skip(
    reason="Graph Workflow replaced by an LLM-orchestrated agent; see test plan in the eval phase."
)

# ---------------------------------------------------------------------------
# Redirect the repository to a throwaway DB *before* importing from app.
# ---------------------------------------------------------------------------
_tmp_dir = tempfile.mkdtemp()
_test_db = os.path.join(_tmp_dir, "leafy_test.db")

import app.storage.repository as _repo_module
_repo_module.DB_PATH = _test_db   # must happen before any other app import

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.tools.geocode import GeocodeResult
from mcp_servers.weather_server import WeatherResult, CurrentWeather, WeatherForecastDay


# ---------------------------------------------------------------------------
# Helpers
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
    """Extract RequestInput interrupt IDs (function_call ids) from events."""
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


from google.adk.models.llm_response import LlmResponse

# Canned LLM response JSON matching ReasonerOutput schema
_REASONER_JSON = json.dumps({
    "next_watering_window": "in 2-3 days, by July 6th",
    "reason": "Soil is still moist from recent watering; mild weather ahead.",
    "moisture_check": "Insert finger 2 cm into soil — water when dry.",
    "is_generic_guidance": True,
})


def _make_llm_response() -> LlmResponse:
    """Build a fake LlmResponse carrying the canned JSON."""
    return LlmResponse(
        content=types.Content(
            role="model",
            parts=[types.Part(text=_REASONER_JSON)],
        ),
        partial=False,
        turn_complete=True,
    )


# ---------------------------------------------------------------------------
# The integration test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_watering_flow():
    """
    Full end-to-end: location → add Basil → last-watered → placement → recommendation.
    """
    _repo_module.init_db()

    # Import after DB redirect
    from app.agent import root_agent

    session_svc = InMemorySessionService()
    runner = Runner(
        app_name="leafy_test",
        agent=root_agent,
        session_service=session_svc,
    )
    session = await session_svc.create_session(
        app_name="leafy_test", user_id="test_user"
    )

    async def run_turn(content: types.Content) -> list:
        evs = []
        async for ev in runner.run_async(
            user_id="test_user",
            session_id=session.id,
            new_message=content,
        ):
            evs.append(ev)
        return evs

    # Mock the LLM response as an async generator of LlmResponse
    async def _fake_llm(*args, **kwargs):
        yield _make_llm_response()

    with (
        patch("app.agent.geocode", return_value=_make_geo_mock()),
        patch("mcp_servers.weather_server.get_weather", return_value=_make_weather_mock()),
        patch(
            "google.adk.models.google_llm.Gemini.generate_content_async",
            side_effect=_fake_llm,
        ),
    ):
        # --- Turn 1: kick off -> main menu prompt ---
        t1 = await run_turn(
            types.Content(role="user", parts=[types.Part(text="start")])
        )
        print(f"\n[T1] events: {[type(e).__name__ for e in t1]}")
        ids1 = _collect_interrupt_ids(t1)
        print(f"[T1] interrupt_ids: {ids1}")
        assert ids1, "T1: Expected RequestInput for the main menu"

        # --- Turn 2: choose "watering advice" -> enters the existing pipeline ---
        t2 = await run_turn(_make_resume_message(ids1[0], "watering advice"))
        print(f"[T2] events: {[type(e).__name__ for e in t2]}")
        ids2 = _collect_interrupt_ids(t2)
        print(f"[T2] interrupt_ids: {ids2}")
        assert ids2, "T2: Expected RequestInput for location"

        # --- Turn 3: provide location ---
        t3 = await run_turn(_make_resume_message(ids2[0], "Dublin, Ireland"))
        print(f"[T3] events: {[type(e).__name__ for e in t3]}")
        ids3 = _collect_interrupt_ids(t3)
        print(f"[T3] interrupt_ids: {ids3}")
        assert ids3, "T3: Expected RequestInput for plant species"

        # --- Turn 4: provide plant species ---
        t4 = await run_turn(_make_resume_message(ids3[0], "Basil"))
        print(f"[T4] events: {[type(e).__name__ for e in t4]}")
        ids4 = _collect_interrupt_ids(t4)
        print(f"[T4] interrupt_ids: {ids4}")
        assert ids4, "T4: Expected RequestInput for last-watered date"

        # --- Turn 5: provide last-watered ---
        t5 = await run_turn(_make_resume_message(ids4[0], "yesterday"))
        print(f"[T5] events: {[type(e).__name__ for e in t5]}")
        ids5 = _collect_interrupt_ids(t5)
        print(f"[T5] interrupt_ids: {ids5}")
        assert ids5, "T5: Expected RequestInput for placement"

        # --- Turn 6: provide placement, flow completes ---
        t6 = await run_turn(_make_resume_message(ids5[0], "indoor"))
        print(f"[T6] events: {[type(e).__name__ for e in t6]}")
        # No more interrupts expected — the workflow should complete
        ids6 = _collect_interrupt_ids(t6)
        print(f"[T6] interrupt_ids (should be empty): {ids6}")

    # --- Assertions ---
    plants = _repo_module.list_plants("local_user")
    assert plants, "Plant catalog is empty — Basil was never saved"
    species_names = [p.species.lower() for p in plants]
    assert any("basil" in s for s in species_names), (
        f"Basil not in catalog. Found: {species_names}"
    )

    profile = _repo_module.get_or_create_profile("local_user")
    assert profile.lat is not None, "Profile lat not saved"
    assert profile.lon is not None, "Profile lon not saved"
    assert abs(profile.lat - 53.3498) < 0.01, f"Unexpected lat: {profile.lat}"

    print("\n✅ Integration test passed!")
    print(f"  Profile: {profile.resolved_name} ({profile.lat}, {profile.lon})")
    print(f"  Plants:  {[p.species for p in plants]}")
