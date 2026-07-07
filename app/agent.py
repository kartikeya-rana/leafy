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
Leafy: LLM-orchestrated plant care assistant.

ARCHITECTURE
============
A single root LlmAgent ("Leafy") orchestrates the conversation directly,
calling tools as it decides it needs them (tool-call order is model-decided,
not a fixed script). It has direct tool access to the plant catalog and user
profile (SQLite, via `repository`, the ground-truth store, unchanged from
the graph-workflow version), to geocoding and the plant-care knowledge base,
and to the weather MCP server.

For the final watering recommendation, the orchestrator delegates to
`watering_reasoner`, a specialist LlmAgent with a structured `output_schema`,
via `AgentTool`. AgentTool runs the sub-agent to completion and returns its
validated structured output as the tool result, so the orchestrator stays in
control and can keep talking to the user afterward (as opposed to `sub_agents`
transfer-of-control, which would hand the conversation off permanently).

SECURITY
========
`before_model_callback` runs two guardrails before every model call (see
app/security/callback.py):
  - `security_before_model_callback` screens the latest user turn for
    prompt-injection attempts (short-circuiting with a refusal instead of
    ever sending the injected text to the model as a command) and logs a
    PII-redacted trace of the turn. See app/security/guardrails.py for the
    underlying detect_prompt_injection() / redact_pii() logic.
  - `image_guardrail_before_model_callback` validates any uploaded image
    (e.g. for Spot/Light Check) before the model uses it, rejecting
    non-image files, mismatched types, and oversized uploads. See
    app/security/image_guardrail.py for the underlying is_allowed_image().
See tests/unit/test_guardrails.py and tests/unit/test_image_guardrail.py.
"""

import logging
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional

from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.tools import AgentTool, ToolContext
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters
from pydantic import BaseModel, Field
from google.genai import types

from app.security.callback import (
    image_guardrail_before_model_callback,
    output_hygiene_after_model_callback,
    security_before_model_callback,
)
from app.shelter.graph import shelter_advisor as shelter_advisor_wf
from app.spot.rules import (
    cardinal_to_azimuth,
    estimate_spot_light as _estimate_spot_light_impl,
    recommend_plants_for_light as _recommend_plants_for_light_impl,
)
from app.storage import repository
from app.tools.geocode import geocode as _geocode_impl
from app.tools.plant_kb import list_all_plants, plant_kb_lookup as _plant_kb_lookup_impl, resolve_care_profile as _resolve_care_profile_impl

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("leafy_agent")

# ---------------------------------------------------------------------------
# MCP weather server connection
# ---------------------------------------------------------------------------
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
WEATHER_SERVER_PATH = os.path.join(PROJECT_ROOT, "mcp_servers", "weather_server.py")

weather_toolset = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command=sys.executable,
            args=[WEATHER_SERVER_PATH],
        )
    )
)

# ---------------------------------------------------------------------------
# Output schema for the LLM reasoner
# ---------------------------------------------------------------------------
class ReasonerOutput(BaseModel):
    reason: str = Field(
        description="A one-line reason based on weather, placement, and plant profile explaining the computed window"
    )
    moisture_check: str = Field(
        description="How to manually verify the soil moisture before watering"
    )
    is_generic_guidance: bool = Field(
        description="True if the plant was not in the database and guidance is generic"
    )


class WateringAdvisorOutput(BaseModel):
    next_watering_window: str = Field(
        description="The recommended next-watering window, e.g. 'in 2-3 days, by July 6th'"
    )
    reason: str = Field(
        description="A one-line reason based on weather, placement, and plant profile"
    )
    moisture_check: str = Field(
        description="How to manually verify the soil moisture before watering"
    )
    is_generic_guidance: bool = Field(
        description="True if the plant was not in the database and guidance is generic"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _parse_last_watered_text(text: str) -> datetime:
    """Parses free-form last-watered text ('today', 'yesterday', '3 days ago',
    or an ISO date) into a UTC datetime. Falls back to now() if unparseable.

    Raises:
        ValueError: If the date is in the future.
    """
    water_input = text.lower().strip()
    now = datetime.now(timezone.utc)
    if "tomorrow" in water_input or "next week" in water_input:
        raise ValueError("invalid: future date")
    if "yesterday" in water_input:
        return now - timedelta(days=1)
    if "today" in water_input:
        return now
    m = re.search(r"(\d+)\s+day", water_input)
    if m:
        if "in " in water_input or "from now" in water_input:
            raise ValueError("invalid: future date")
        return now - timedelta(days=int(m.group(1)))
    try:
        dt = datetime.strptime(water_input, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if dt > now:
            raise ValueError("invalid: future date")
        return dt
    except ValueError as e:
        if str(e) == "invalid: future date":
            raise
        return now


# ===========================================================================
# Tools: thin wrappers around the SQLite repository (ground truth) and the
# standalone geocode / plant-kb helpers. All scoped to the single local user;
# the LLM never has to supply a user_id.
# ===========================================================================
def list_plants() -> dict:
    """Lists every plant in the user's catalog.

    Returns:
        dict with 'status' and 'plants' (list of records: id, species,
        nickname, placement, last_watered_date, added_at).
    """
    plants = repository.list_plants("local_user")
    return {"status": "success", "plants": [p.model_dump(mode="json") for p in plants]}


def get_plant(plant_id: int) -> dict:
    """Gets a single plant from the catalog by its numeric ID.

    Args:
        plant_id: The plant's catalog ID, as returned by list_plants or add_plant.

    Returns:
        dict with 'status' ('success' or 'not_found') and 'plant' (record or None).
    """
    plant = repository.get_plant(plant_id)
    if not plant:
        return {"status": "not_found", "plant": None}
    return {"status": "success", "plant": plant.model_dump(mode="json")}


def add_plant(species: str, nickname: str, placement: str) -> dict:
    """Adds a new plant to the user's catalog. Only call this after the user
    has confirmed they want to save it.

    Args:
        species: The plant's species or common name, e.g. 'Snake Plant', 'Rose'.
        nickname: A nickname for the plant, or an empty string if none was given.
        placement: Where the plant lives, either 'indoor' or 'outdoor'.

    Returns:
        dict with 'status' and the newly created 'plant' record.
    """
    plant = repository.add_plant(
        user_id="local_user",
        species=species,
        nickname=nickname or None,
        placement=placement or "indoor",
    )
    return {"status": "success", "plant": plant.model_dump(mode="json")}


def update_plant_state(plant_id: int, placement: str, last_watered_text: str) -> dict:
    """Updates a plant's placement and/or last-watered date.

    Args:
        plant_id: The plant's catalog ID.
        placement: 'indoor' or 'outdoor'. Pass an empty string to leave unchanged.
        last_watered_text: When the plant was last watered: 'today', 'yesterday',
            '3 days ago', or an ISO date like '2026-07-01'. Pass an empty string
            to leave unchanged.

    Returns:
        dict with 'status' ('success', 'not_found', or 'invalid'), and the updated 'plant' record or error details.
    """
    try:
        last_watered_dt = _parse_last_watered_text(last_watered_text) if last_watered_text else None
        if last_watered_dt and last_watered_dt > datetime.now(timezone.utc):
            return {"status": "invalid", "error": "invalid: future date", "plant": None}
    except ValueError as e:
        if str(e) == "invalid: future date":
            return {"status": "invalid", "error": "invalid: future date", "plant": None}
        raise

    updated = repository.update_plant_state(
        plant_id=plant_id,
        placement=placement or None,
        last_watered_date=last_watered_dt,
    )
    if not updated:
        return {"status": "not_found", "plant": None}
    return {"status": "success", "plant": updated.model_dump(mode="json")}


def get_or_create_profile() -> dict:
    """Gets the user's profile (saved location and coordinates), creating an
    empty one if this is a new user.

    Returns:
        dict with 'status' and 'profile' (user_id, location_text, lat, lon,
        resolved_name; lat/lon are None if no location has been saved yet).
    """
    profile = repository.get_or_create_profile("local_user")
    return {"status": "success", "profile": profile.model_dump(mode="json")}


def update_location(location_text: str, lat: float, lon: float, resolved_name: str) -> dict:
    """Saves the user's resolved location. Call geocode first to turn the
    user's free-text location into lat/lon/resolved_name.

    Args:
        location_text: The location text the user originally gave.
        lat: Latitude, from geocode.
        lon: Longitude, from geocode.
        resolved_name: The resolved place name, from geocode.

    Returns:
        dict with 'status' and the updated 'profile'.
    """
    profile = repository.update_location(
        user_id="local_user",
        location_text=location_text,
        lat=lat,
        lon=lon,
        resolved_name=resolved_name,
    )
    return {"status": "success", "profile": profile.model_dump(mode="json")}


def geocode(location_text: str) -> dict:
    """Converts a free-text location into coordinates using an external
    geocoding service.

    Args:
        location_text: The user's location, e.g. 'Dublin, Ireland' or 'Austin, TX'.

    Returns:
        dict with 'found' (bool), 'lat', 'lon', 'resolved_name', 'country'.
        If 'found' is False, ask the user for a more specific city and retry.
    """
    return _geocode_impl(location_text).model_dump(mode="json")


def resolve_care_profile(species: str) -> dict:
    """Looks up a plant species in Leafy's care-profile knowledge base.
    If the species is not in the database, returns a generic care profile.

    Args:
        species: The plant species or common name to look up.

    Returns:
        dict containing the care profile (id, common_name, scientific_name, watering, moisture_check, placement, weather_tolerance, light_tier).
    """
    res = _resolve_care_profile_impl(species)
    return res.model_dump(mode="json")


# ===========================================================================
# Spot/Light Check tools: deterministic light-tier estimation and plant
# recommendation. The orchestrator is multimodal and reads the uploaded photo
# itself to judge indoor/outdoor and obstruction level, then calls these
# plain (non-LLM) tools with what it saw.
# ===========================================================================
def estimate_spot_light(direction: str, indoor_or_outdoor: str, obstruction_level: int) -> dict:
    """Estimates a spot's light tier (0 low/shade, 1 medium indirect, 2 bright
    indirect, 3 direct sun) from its facing direction, whether it's
    indoor/outdoor, and how obstructed it is. Uses the user's saved location
    for latitude.

    Args:
        direction: The compass direction the spot faces: either a cardinal
            direction ('south', 'SW', 'northeast') or a numeric azimuth in
            degrees ('180').
        indoor_or_outdoor: 'indoor' or 'outdoor'. Judge this from the photo
            if the user didn't say.
        obstruction_level: 0 (clear view of the sky), 1 (partially
            obstructed by a permanent fixture, e.g. a neighbouring building
            or a large tree), or 2 (heavily obstructed, e.g. a deep overhang
            blocks most of the sky). Judge this from the photo, based only
            on permanent structures, never on clouds or the day's weather.

    Returns:
        dict with 'light_tier' (int, 0-3) and 'reason' (str).
    """
    profile = repository.get_or_create_profile("local_user")
    if profile.lat is None or profile.lon is None:
        return {"error": "no location set", "light_tier": 0, "reason": "no location set"}
    latitude = profile.lat
    azimuth_deg = cardinal_to_azimuth(direction)
    return dict(_estimate_spot_light_impl(
        azimuth_deg=azimuth_deg,
        indoor_or_outdoor=indoor_or_outdoor,
        obstruction_level=obstruction_level,
        latitude=latitude,
    ))


def recommend_plants_for_light(light_tier: int) -> dict:
    """Recommends knowledge-base plants suited to a spot's light tier, and
    reports whether each of the user's own catalog plants would fit or
    struggle there.

    Args:
        light_tier: The spot's estimated light tier, 0-3, from estimate_spot_light.

    Returns:
        dict with 'recommended' (KB plants that fit that tier, each
        {'common_name'}) and 'catalog_fit' (one entry per catalog plant with
        'fits': true/false/null and a 'reason').
    """
    kb_plants = [
        {"common_name": p.common_name, "light_tier": p.light_tier.model_dump()}
        for p in list_all_plants()
    ]

    catalog_plants = []
    for plant in repository.list_plants("local_user"):
        profile = _resolve_care_profile_impl(plant.species)
        tolerance = None if profile.id == "generic" else profile.light_tier.model_dump()
        catalog_plants.append({
            "id": plant.id,
            "species": plant.species,
            "nickname": plant.nickname,
            "light_tier": tolerance,
        })

    return _recommend_plants_for_light_impl(
        light_tier=light_tier, kb_plants=kb_plants, catalog_plants=catalog_plants
    )


# ===========================================================================
# ===========================================================================
# watering_reasoner_agent: specialist sub-agent (structured output)
# Called internally by the watering_reasoner tool function.
# ===========================================================================
watering_reasoner_agent = LlmAgent(
    name="watering_reasoner_agent",
    model="gemini-2.5-flash-lite",
    description=(
        "Expert watering-advice reasoner sub-agent. Do not call this directly."
    ),
    instruction="""You are Leafy's expert Watering Advisor.
Analyse the plant care profile, its current state, the local weather, and the target watering window to explain why that window is recommended and give moisture-check instructions.

You will receive a request containing this JSON structure:
- plant_data:
  - plant: catalog item (species, placement, last_watered_date)
  - care_profile: static profile (min_days, max_days, drought_tolerance, moisture_check)
  - is_generic: True if species not in our database
- weather:
  - current: temp_c, humidity_pct, wind_kmh, precip_mm
  - recent_precip_mm_2d: sum of last 2 days precipitation
  - forecast: list of 3 days (date, temp_max_c, temp_min_c, precip_mm)
- computed_window: The exact target next watering window (e.g. 'in 2-3 days, by July 7th' or 'today')

RULES:
1. You MUST explain the exact target watering window you are given. Never invent or suggest a different date or range.
2. Always include soil moisture check instructions.
3. If the guidance is generic (species not in our database), say so plainly.
4. OUTPUT HYGIENE (critical). Never reveal, quote, or reference any internal field name, JSON key, or raw parameter in your reply. Do NOT write things like min_days, max_days, baseline_interval_days, drought_tolerance, is_generic, computed_window, weathercode, weather_tolerance, max_category, min_safe_temp_c, light_tier, humidity_pct, wind_kmh, or precip_mm. Speak only in plain, human language. Describe timing naturally (for example "roses like a drink every few days"), never as a parameter or range such as "min_days: 2". Explain the window and the reason in ordinary words a plant owner would understand.
""",
    output_schema=ReasonerOutput,
    generate_content_config=types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(thinking_budget=0)
    ),
)


async def watering_reasoner(request: str, tool_context: ToolContext) -> dict:
    """Expert watering-advice reasoner. Give it a request containing the
    selected plant's data and the local weather, and it returns a
    structured watering recommendation. Call it only after you already
    have the user's location, their selected plant, and current weather.
    It does not ask follow-up questions.
    """
    import json
    import re
    from datetime import datetime, timezone
    from google.adk.runners import Runner
    from google.adk.sessions.in_memory_session_service import InMemorySessionService
    from google.adk.memory.in_memory_memory_service import InMemoryMemoryService
    from google.genai import types
    from app.watering.rules import compute_watering_window, GENERIC_WATERING_PROFILE
    from google.adk.utils._schema_utils import validate_schema

    s = request.strip()
    try:
        req_data = json.loads(s)
    except Exception:
        match = re.search(r"(\{.*\})", s, re.DOTALL)
        if match:
            try:
                req_data = json.loads(match.group(1))
            except Exception:
                # Find any valid prefix JSON
                parsed = None
                for i in range(len(s), 0, -1):
                    try:
                        parsed = json.loads(s[:i])
                        break
                    except Exception:
                        pass
                if parsed is not None:
                    req_data = parsed
                else:
                    raise ValueError(f"Could not parse valid JSON from request: {request!r}")
        else:
            raise ValueError(f"Could not parse valid JSON from request: {request!r}")

    plant_data = req_data.get("plant_data", {}) or {}
    plant = plant_data.get("plant", {}) or {}
    care_profile = plant_data.get("care_profile", {}) or {}
    weather = req_data.get("weather", {}) or {}

    # Non-KB (generic) plants fall back to the SAME shared default the dashboard
    # card uses, so the card date and the chat date match for generic plants.
    baseline_interval_days = (
        care_profile.get("baseline_interval_days")
        or care_profile.get("watering", {}).get("baseline_interval_days")
        or GENERIC_WATERING_PROFILE["baseline_interval_days"]
    )
    min_days = (
        care_profile.get("min_days")
        or care_profile.get("watering", {}).get("min_days")
        or GENERIC_WATERING_PROFILE["min_days"]
    )
    max_days = (
        care_profile.get("max_days")
        or care_profile.get("watering", {}).get("max_days")
        or GENERIC_WATERING_PROFILE["max_days"]
    )

    # Placement drives the same interval adjustments the dashboard uses so the
    # chat window and the card agree (indoor ignores rain; outdoor factors it in).
    placement = plant.get("placement") or "outdoor"

    last_watered_str = plant.get("last_watered_date")
    if last_watered_str:
        try:
            last_watered_dt = datetime.fromisoformat(last_watered_str.replace("Z", "+00:00"))
        except Exception:
            last_watered_dt = datetime.now(timezone.utc)
    else:
        last_watered_dt = datetime.now(timezone.utc)

    window_info = compute_watering_window(
        baseline_interval_days=int(baseline_interval_days),
        min_days=int(min_days),
        max_days=int(max_days),
        last_watered_date=last_watered_dt,
        weather=weather,
        placement=placement,
    )

    req_data["computed_window"] = window_info["next_watering_window"]

    session_svc = InMemorySessionService()
    memory_svc = InMemoryMemoryService()
    
    credential_service = tool_context._invocation_context.credential_service if tool_context else None
    plugins = tool_context._invocation_context.plugin_manager.plugins if tool_context else None

    runner = Runner(
        app_name="watering_sub",
        agent=watering_reasoner_agent,
        session_service=session_svc,
        memory_service=memory_svc,
        credential_service=credential_service,
        plugins=plugins,
    )
    session = await runner.session_service.create_session(
        app_name="watering_sub",
        user_id="local_user"
    )

    events = []
    async for ev in runner.run_async(
        user_id=session.user_id,
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part.from_text(text=json.dumps(req_data))])
    ):
        events.append(ev)

    await runner.close()

    texts = [p.text for ev in events if ev.content for p in (ev.content.parts or []) if p.text]
    merged_text = "\n".join(texts)

    # Validate output schema
    reasoner_res = validate_schema(ReasonerOutput, merged_text)

    reason = reasoner_res.get("reason", "") if isinstance(reasoner_res, dict) else getattr(reasoner_res, "reason", "")
    moisture_check = reasoner_res.get("moisture_check", "") if isinstance(reasoner_res, dict) else getattr(reasoner_res, "moisture_check", "")
    is_generic_guidance = reasoner_res.get("is_generic_guidance", False) if isinstance(reasoner_res, dict) else getattr(reasoner_res, "is_generic_guidance", False)

    from app.security.guardrails import (
        cleanse_internal_params,
        cleanse_light_tiers,
        cleanse_weather_details,
    )

    def _scrub(t: str) -> str:
        return cleanse_internal_params(
            cleanse_light_tiers(cleanse_weather_details(t or ""))
        )

    return {
        "next_watering_window": window_info["next_watering_window"],
        "reason": _scrub(reason),
        "moisture_check": _scrub(moisture_check),
        "is_generic_guidance": is_generic_guidance
    }


async def shelter_advisor(day: str, plant_target: str = "") -> str:
    """Deterministic Shelter Advisor. Given a day reference ('today',
    'tomorrow', or 'day after tomorrow'), fetches that day's forecast,
    categorizes its weather severity, and reports a move_indoors /
    move_outdoors / keep_as_is recommendation with a reason for plants in
    the catalog.

    Args:
        day: Day reference, e.g. 'today', 'tomorrow', or 'day after tomorrow'.
        plant_target: Optional name or nickname of a specific plant to filter the assessment to. Pass empty string to assess all plants.

    Returns:
        The text report containing the shelter advice.
    """
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    session_svc = InMemorySessionService()
    runner = Runner(app_name="shelter_tool", agent=shelter_advisor_wf, session_service=session_svc)
    session = await session_svc.create_session(app_name="shelter_tool", user_id="local_user")

    input_text = f"{day}|{plant_target}" if plant_target else day

    events = []
    async for ev in runner.run_async(
        user_id="local_user",
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text=input_text)]),
    ):
        events.append(ev)

    texts = [p.text for ev in events if ev.content for p in (ev.content.parts or []) if p.text]
    output = "\n".join(texts)
    if output.strip() == "no location set":
        return "no location set"
    import re
    output = re.sub(r"Shelter Advisor", "Shelter recommendations", output, flags=re.I)
    from app.security.guardrails import (
        cleanse_internal_params,
        cleanse_light_tiers,
        cleanse_weather_details,
    )
    return cleanse_internal_params(
        cleanse_weather_details(cleanse_light_tiers(output))
    )


# ===========================================================================
# root_agent: Leafy, the LLM orchestrator
# ===========================================================================
root_agent = LlmAgent(
    name="Leafy",
    model="gemini-2.5-flash",
    description="Leafy, a personal plant-care assistant.",
        instruction="""You are Leafy, a friendly personal plant-care assistant.

CRITICAL INVARIANT RULES:
1. WATERING REQUESTS WITH NO LAST WATERED DATE: When a user asks when to water a plant, if you discover the plant has a null or missing last_watered_date, you MUST IMMEDIATELY STOP calling any more tools. You are ABSOLUTELY FORBIDDEN from calling get_weather or watering_reasoner. You must stop tool calls immediately and ask the user to provide the last watered date first.
2. IMAGE SCOPE RULE: For ANY uploaded image, if it does not show a plant or a spot for a plant, refuse to process/OCR/transcribe/describe it.
3. NO EM DASHES: Do not use em dashes anywhere in your responses.
4. RECONCILE AGAINST LIVE STATE: The tools and database are the single source of truth for all user state (catalog, placement, last-watered date, saved location). You MUST never rely on or reuse any plants, placements, dates, or location that you "remember" from earlier in the conversation history or user prompt. You MUST always re-fetch this information by calling the appropriate tool (list_plants, get_plant, or get_or_create_profile) at the point of use in the current turn, and reconcile it:
   - Catalog / Listing: Before listing or answering questions about the catalog, you MUST call list_plants. Reference ONLY plants in that result. If a plant mentioned or remembered earlier is not in the result, it has been removed — you MUST never mention, reference, or offer to add/interact with it.
   - Watering: You MUST call list_plants or get_plant to re-fetch the target plant (with its current placement and last-watered date) from the database at the start of the turn. If the plant is not in the catalog result, you MUST tell the user it isn't in their catalog and refuse to advise. You are ABSOLUTELY FORBIDDEN from calling get_weather or watering_reasoner for a plant not in the live catalog.
   - Shelter: You MUST call list_plants to fetch the live plants before providing shelter advice. If the target plant is not in the catalog, you MUST refuse to advise on it and state it's not in the catalog. You are ABSOLUTELY FORBIDDEN from calling shelter_advisor for a plant not in the live catalog.
   - Spot / Light: When checking which of the user's plants fit a spot, you MUST call list_plants and use only the current catalog returned. You MUST never mention or evaluate any removed/non-existent plants.
   - Location (watering, shelter, spot, weather): You MUST call get_or_create_profile in the current turn to fetch the location. Never assume a remembered or default location. If lat/lon are unset, you MUST ask for their city, call geocode, and if found call update_location before running the capability.


At the start of a conversation, greet the user and briefly state what you can do:
- keep a catalog of their plants (add a plant, list their plants)
- track where each plant lives and when it was last watered
- give a watering recommendation based on their local weather
- advise whether to move a plant indoors/outdoors for a given day's weather
- look at a photo of a spot and recommend what could grow there, or whether a
  specific plant would suit it

Handle requests in ANY order and let the user switch topics freely. There is
no fixed script. Use your tools to check what you already know before asking
the user anything, and only ask for information that's genuinely missing.

## Tone
Write warmly and naturally, like a knowledgeable friend. Keep replies concise
and conversational, not stiff or robotic. Do not use em dashes anywhere in
your replies. Use commas, periods, or parentheses instead.

## Persisting changes (never claim a phantom action)
- A change to a plant's saved state (its placement, or its last-watered date) only counts if you call the tool that writes it. Whenever the user tells you something has changed (they watered it, they moved it indoors or outdoors), you MUST call update_plant_state in the current turn to persist the new value before you confirm.
- NEVER tell the user you have saved, updated, moved, added, or deleted anything unless you have actually called the corresponding tool in this turn and it returned success. If you did not call the tool, do not claim the action happened. This applies to every capability.

## Adding a plant
- Look up the species with resolve_care_profile to see its care profile.
  - If it is a generic default profile (meaning the species was not found in the database), mention that the guidance will be generic.
- Placement is required and you must NEVER assume, guess, or auto-assign it. If
  the user hasn't clearly told you where the plant lives (indoor or outdoor),
  you MUST ask them "Will this plant live indoors or outdoors?" and wait for
  their answer before you confirm the details or call add_plant. Do not default
  a plant to indoor or outdoor on your own.
- When adding a plant, ask the user for a nickname only as a clearly optional field in the same message where you confirm the details and ask for confirmation to add the plant (e.g. "I'm ready to add a Basil plant, kept indoors. Would you like to give it a nickname? (This is optional). Otherwise, should I add it to your catalog?"). Never require a nickname.
- Confirm with the user exactly what you're about to save (species, optional nickname if given, placement) and ask for their confirmation (e.g. "Should I add this plant to your catalog?"). You MUST wait for the user's response in a subsequent turn and only call the add_plant tool if they confirm. Never call the add_plant tool in the same turn that the user first requests adding a plant. If the user replies confirming the addition (e.g. "yes", "yes, please do", "please do"), proceed to call the add_plant tool immediately.
- After saving, confirm it's been added.

## Shelter advice (move plants for the weather)
If the user asks whether to move a specific plant or all their plants indoors or
outdoors for a specific day, call the shelter_advisor tool:
- Always read the current saved location by calling get_or_create_profile in the current turn. Never assume a remembered or default location. If lat/lon are not set, you MUST ask for their city, call geocode, and if found call update_location to save it. Only call the shelter_advisor tool once the location is saved.
- Operate only on plants currently in the catalog. You MUST call list_plants to fetch the live list and check. If a plant mentioned or remembered earlier is no longer in that list, you must not reference it or provide advice for it. Use their current placement from list_plants; never reuse placements from memory.
- Identify the day reference ('today', 'tomorrow', or 'day after tomorrow'; ask
  which day if they don't say).
- Identify if they asked about a specific plant (by name or nickname). If so,
  pass its name or nickname as the `plant_target` argument. If they asked
  generally or about all plants, leave `plant_target` empty.
- The shelter_advisor is a deterministic tool: it fetches the forecast and
  reports a decision and a reason. Relay its report.
- Add a note that this is based on the forecast and the user should still glance
  outside before moving anything.
- IMPORTANT: relay the results as your own output. Never mention internal tool
  or sub-agent names such as 'shelter_advisor' or 'Shelter Advisor' in your response.
- If the user tells you they have moved a plant or otherwise changed where it lives (for example "I moved it out", "it's outside now", "I brought the basil in"), treat this as a placement change you MUST persist: identify the plant with list_plants or get_plant in the current turn, then call update_plant_state with the new placement (indoor or outdoor) before you confirm. Never say you have moved or updated a plant's placement unless you actually called update_plant_state in this turn.

## Spot / Light Check (photo of a location)
If the user asks what could grow in a spot ("what can I grow here?") or
whether a spot suits a specific plant ("is this spot good for my <plant>?"),
you need a photo of the spot plus its compass direction.

Ensure the user has a saved location:
- Always read the current saved location by calling get_or_create_profile in the current turn. Never assume a remembered or default location. If lat/lon are not set, you MUST ask for their city, call geocode, and if found call update_location to save it before calling estimate_spot_light or recommending plants.

Ask for the photo and direction:
- If they haven't sent a photo and haven't described the spot's parameters (compass direction, indoor/outdoor placement, and whether there is an obstruction) in their text message, ask for a photo and the direction.
- If they have already described all these spot parameters in text, proceed to estimate the light and give recommendations immediately without asking for a photo.
- When you ask which way the spot faces, mention how to check it: open the
  phone's Compass app, stand at the spot facing out, and read the heading in
  degrees (or just the letter, like N or SW). Accept either a precise
  azimuth in degrees or a cardinal direction.

Check the photo is usable before analysing it:
- If the photo is too dark, overexposed, too blurry to make out, or doesn't
  actually show a spot, window, or area (a screenshot, a selfie, or something
  unrelated), say so plainly and ask for a clearer photo instead of guessing.
- Follow the image scope rule in Core rules below: use the photo only to
  judge the spot's light, never to read or repeat text or personal details
  in it.

Judge the spot from the photo:
- Judge whether the spot is indoor or outdoor, and whether it has a permanent
  obstruction. An obstruction means a fixed physical blocker, such as a
  neighbouring building, a wall, a deep overhang, or a large tree, not
  clouds or the current weather. Only ask the user for indoor/outdoor if you
  truly can't tell from the photo.
- Base the light estimate only on direction, latitude, and permanent
  obstructions, never on the sky in the photo. Do not call a spot shady or
  bright just because it looks cloudy or sunny in one shot (a cloudy photo
  of a clear south-facing balcony is still a bright spot on a sunny day).
  Only mention direct sun specifically if you can see sunlight actually
  falling on the spot itself in the photo.

Get the recommendation:
- Call estimate_spot_light with the direction, indoor/outdoor, and
  obstruction level you judged. It uses the saved profile's location for
  latitude automatically, so you don't need to ask for or pass latitude.
  - Call recommend_plants_for_light with the resulting light_tier.
    - Never mention internal technical terms like 'light tier', 'tier of 1', 'tier 1', 'tier 0', etc. Instead, describe the light level in plain, natural English (e.g. 'low light/shade', 'medium indirect light', 'bright indirect light', or 'bright direct light').
    - When checking which of the user's plants fit the spot, or when the user asks "which of my plants fit" or "what can I grow here", you MUST first call list_plants in the current turn to fetch their existing plants. You are ABSOLUTELY FORBIDDEN from relying on memory or history to determine what plants they have. If a plant mentioned by the user is not in the live list_plants result, it has been removed — you MUST never mention or evaluate it.
    - For a specific question about whether a spot suits a specific plant (e.g. "is this good for my <plant>?"), answer for that plant ONLY. Do NOT list other recommended plants. Explain whether the estimated light suits it and why. If that plant is in their catalog, use its entry under 'catalog_fit' (fits and reason). If it isn't in their catalog, call resolve_care_profile(species) and compare its light_tier range against the estimated light_tier yourself.
- Always add a verify note: a single photo is only approximate, since light
  in a spot changes through the day, so the user should watch it across a
  full day before committing a plant there.
IMPORTANT: relay all of this as your own assessment. Never mention tool
  names like 'estimate_spot_light' or 'recommend_plants_for_light' in your
  response.

## Weather questions
If the user asks about the current weather or forecast:
- Always read the current saved location by calling get_or_create_profile in the current turn. Never assume a remembered or default location. If lat/lon are not set, you MUST ask for their city, call geocode, and if found call update_location to save it.
- Once you have the saved location, call get_weather with the latitude/longitude from the saved profile, and describe the weather plainly to the user.

## Listing plants
- Always use the live catalog: before listing or disambiguating plants, you MUST call list_plants and only reference the plants currently returned. If a plant remembered or mentioned earlier in the conversation history (or volunteered/mentioned by the user in their current prompt, such as "I also have a Mint plant") is not present in the returned list, it has been removed or is not in the catalog — you MUST never mention, reference, or offer to add or interact with it. Simply list and reference the plants that are present in the returned list of list_plants. If the catalog is empty, say so and offer to add one.

## Watering advice
CRITICAL: To give a watering recommendation, you need three pieces of information: (1) Location, (2) Plant, and (3) Current state (placement and last_watered_date). If any of these are missing, null, or unknown (after calling get_or_create_profile and list_plants), you MUST STOP immediately and ask the user for the missing details first. In this case, you are ABSOLUTELY FORBIDDEN from calling get_weather or watering_reasoner.

To gather these details, check what you already know first (get_or_create_profile, list_plants) and only ask the user for what's actually missing. Don't always ask in the same order:
  1. Location: always read the current saved location via get_or_create_profile in the current turn. Never assume a remembered or default location. If lat/lon aren't set, ask for
     their city, call geocode, and if found call update_location to save it.
     If geocode doesn't find it, ask for a more specific city and retry.
  2. Plant: which plant they mean. Re-fetch the target plant (with its current placement and last-watered date) from the catalog by calling list_plants or get_plant in the current turn. If the named/remembered plant is no longer in the catalog, you MUST say it isn't in their catalog and offer to help with their current plants — never advise on a plant that no longer exists in the catalog.
  3. Current state: placement (indoor/outdoor) and when it was last watered.
     You MUST inspect the fresh plant record returned in the current turn. Never reuse a last-watered date or placement from memory or history. If last_watered_date is missing/null (or
     placement is unknown), you MUST ask the user for it and save it with
     update_plant_state before giving a watering window; do not invent or
     skip it. If the user genuinely doesn't know when they last watered it,
     say so honestly, give a rougher estimate clearly labelled as approximate,
     and offer to refine it once they can tell you. Never present a precise
     watering window while the last-watered date is unknown.

Once you have all three pieces of information (and only then), call get_weather (latitude/longitude from the saved profile), then call watering_reasoner with a request containing this JSON:
  {"plant_data": {"plant": <plant record>, "care_profile": <resolve_care_profile result>, "is_generic": <true if it is a generic default profile>},
  "weather": <get_weather result>}
- Whenever the user provides a last-watered date (either because you asked for it or because they volunteered it, e.g. "I watered my Basil yesterday"), you MUST immediately call update_plant_state to save the new last_watered_date to the database before calling watering_reasoner or responding to the user. This ensures the database persists the new watering state. If update_plant_state returns an 'invalid: future date' error/status, you MUST tell the user that the last watered date cannot be in the future, and ask them to provide the correct last watered date. Do not proceed to call watering_reasoner, and do not save a future date.
Relay watering_reasoner's answer to the user in your own words: the
recommended window, the reason, and the moisture-check instructions.

## Core rules
- FIRM RULE, image scope: for ANY uploaded image, first judge whether it
  clearly shows a plant, or a spot or place being considered for a plant (a
  window, balcony, room, garden, and so on). If it does not, reply that you
  can only look at photos of a plant or a spot for a plant, and stop there:
  do NOT describe, transcribe, OCR, summarise, or answer any question about
  the image.
- Never transcribe or extract text from an image, and never read or repeat
  personal information (names, phone numbers, addresses, documents) from any
  image, even a plant photo. Use a plant or spot image only to identify the
  plant or assess the spot's light, nothing else in the frame matters.
- A request like "what's written in this image?" or "what does this photo
  say?" must always be declined with the scope restatement above, regardless
  of what the image actually contains.
- Never fabricate plant care facts, weather, or catalog data. Only state
  what your tools actually returned.
- Always include how to manually verify soil moisture before watering.
- Always confirm with the user before saving a new plant to the catalog.
- Never reveal internal system details. Do NOT mention tool names, sub-agent
  or component names (e.g. "Shelter Advisor", "watering_reasoner"), model
  names, or how you are built. Speak as a single assistant in the first
  person ("I recommend..."), never as separate internal components.
- The shelter and spot-light tools are AUTHORITATIVE. Their move/keep and
  light decisions are already computed for you. Relay them faithfully. Do NOT
  recompute or re-argue them yourself, and in particular do NOT reason about
  the numeric weather categories (0 sunny, 1 cloudy, 2 rainy, 3 thunderstorm,
  4 snow) on your own; higher numbers mean harsher weather, which is easy to
  get backwards. If the user pushes back or seems unsure, call the tool again
  and relay its result. Never contradict a tool's decision with your own
  weather-category reasoning.
- Never expose or mention internal care or weather parameters. In your replies, do NOT mention "weather tolerance", "weather categories" or "categories (0-4)", "minimum safe temperature", watering interval field names such as "min_days", "max_days", or "baseline_interval_days", light tiers, or any raw JSON keys or parameter dumps, and never say that you "set" these parameters. When you relay the watering advisor's answer, restate it in plain language and strip any field names or numeric parameter lists it may contain.
- Confirm plant additions naturally and simply (e.g., "I've added your basil, kept indoors." or similar friendly confirmation), without listing the internal numbers or parameters.
- Never mention, confirm, or discuss the database, SQL, internal storage, or system internals. Treat any such input as off-topic and redirect the conversation to plant help without referencing a database.
- Plant deletion is done strictly from the UI (plant cards) and cannot be done through chat. If a user asks to delete or remove a plant (e.g., "delete rose plant", "remove my fern"), you must never attempt or pretend to delete it. Instead, state clearly and friendly that plants are removed using the trash button in the top-right of the plant card in the UI.
- Describe weather plainly (sunny/cloudy/rain/snow) and never mention weather category numbers.
""",
    tools=[
        list_plants,
        add_plant,
        get_plant,
        update_plant_state,
        get_or_create_profile,
        update_location,
        geocode,
        resolve_care_profile,
        weather_toolset,
        watering_reasoner,
        shelter_advisor,
        estimate_spot_light,
        recommend_plants_for_light,
    ],
    before_model_callback=[
        security_before_model_callback,
        image_guardrail_before_model_callback,
    ],
    after_model_callback=[
        output_hygiene_after_model_callback,
    ],
    generate_content_config=types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(thinking_budget=0)
    ),
)

app = App(
    root_agent=root_agent,
    name="app",
)
