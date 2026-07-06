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
Shelter Advisor — deterministic ADK graph.

fetch_forecast -> categorize -> assess_plants -> report

No RequestInput anywhere: the whole thing runs in one pass per invocation.
Leafy (app/agent.py) delegates to this graph via AgentTool when the user asks
about moving/sheltering plants for a given day. All the actual decisions are
made by the pure functions in app/shelter/rules.py — this module is just the
ADK plumbing (fetching the forecast, threading data between nodes, and
formatting the final report).
"""

import logging
from typing import Any

from google.adk.agents.context import Context
from google.adk.events.event import Event
from google.adk.workflow import Workflow, node
from google.genai import types

from app.shelter.rules import (
    CATEGORY_NAMES,
    assess_plant,
    categorize_weather,
    resolve_forecast_day_index,
)
from app.storage import repository
from app.tools.plant_kb import resolve_care_profile

logger = logging.getLogger("leafy_agent")

# Fallback tolerance for plants with no KB match and no stored estimate
# (e.g. added before the Shelter Advisor feature existed).
_DEFAULT_TOLERANCE: dict = {"max_category": 1, "min_safe_temp_c": 10}

def _extract_text(node_input: Any) -> str:
    """Pulls plain text out of a types.Content (what START hands the first
    node) or returns node_input unchanged if it's already a string."""
    if isinstance(node_input, str):
        return node_input
    parts = getattr(node_input, "parts", None) or []
    return "".join(p.text for p in parts if getattr(p, "text", None))


# Node 1 — fetch_forecast
# ===========================================================================
@node
async def fetch_forecast(ctx: Context, node_input: Any):
    logger.info("[shelter/fetch_forecast] Entry.")

    input_text = _extract_text(node_input).strip()
    if "|" in input_text:
        day_text, plant_target = input_text.split("|", 1)
        day_text = day_text.strip()
        plant_target = plant_target.strip()
    else:
        day_text = input_text
        plant_target = ""

    day_text = day_text or "today"

    try:
        day_index = resolve_forecast_day_index(day_text)
    except ValueError:
        logger.warning(f"[shelter/fetch_forecast] Unrecognized day '{day_text}'; defaulting to today.")
        day_index = 0

    profile = repository.get_or_create_profile("local_user")
    if profile.lat is None or profile.lon is None:
        yield Event(output={"error": "no location set"})
        return

    lat = profile.lat
    lon = profile.lon

    from mcp_servers.weather_server import get_weather
    try:
        weather_res = get_weather(latitude=lat, longitude=lon)
        forecast_day = weather_res.forecast[day_index]
        day_data = forecast_day.model_dump(mode="json")
    except Exception as e:
        logger.error(f"[shelter/fetch_forecast] Weather lookup failed: {e}")
        day_data = {
            "date": "unknown",
            "temp_max_c": 15.0,
            "temp_min_c": 10.0,
            "precip_mm": 0.0,
            "weathercode": 0,
        }

    yield Event(output={"day": day_data, "day_text": day_text, "plant_target": plant_target})


# ===========================================================================
# Node 2 — categorize
# ===========================================================================
@node
async def categorize(ctx: Context, node_input: dict):
    logger.info("[shelter/categorize] Entry.")
    if "error" in node_input:
        yield Event(output=node_input)
        return

    day = node_input.get("day", {})
    day_text = node_input.get("day_text", "today")
    plant_target = node_input.get("plant_target", "")

    try:
        category = categorize_weather(day.get("weathercode", 0))
    except ValueError as e:
        logger.warning(f"[shelter/categorize] {e}; defaulting to category 1 (cloudy).")
        category = 1

    yield Event(output={
        "category": category,
        "day_temp_min": day.get("temp_min_c", 10.0),
        "date": day.get("date", "unknown"),
        "day_text": day_text,
        "plant_target": plant_target,
    })


# ===========================================================================
# Node 3 — assess_plants
# ===========================================================================
@node
async def assess_plants(ctx: Context, node_input: dict):
    logger.info("[shelter/assess_plants] Entry.")
    if "error" in node_input:
        yield Event(output=node_input)
        return

    category = node_input["category"]
    day_temp_min = node_input["day_temp_min"]
    plant_target = node_input.get("plant_target", "").strip().lower()

    plants = repository.list_plants("local_user")
    if plant_target:
        filtered = []
        for p in plants:
            name_match = p.nickname and plant_target in p.nickname.lower()
            species_match = plant_target in p.species.lower()
            id_match = False
            try:
                id_match = (int(plant_target) == p.id)
            except ValueError:
                pass
            if name_match or species_match or id_match:
                filtered.append(p)
        plants = filtered

    assessments = []
    for plant in plants:
        tolerance = resolve_care_profile(plant.species).weather_tolerance.model_dump()
        result = assess_plant(
            day_category=category,
            day_temp_min=day_temp_min,
            tolerance=tolerance,
            placement=plant.placement.value,
        )
        assessments.append({
            "plant_id": plant.id,
            "species": plant.species,
            "nickname": plant.nickname,
            "placement": plant.placement.value,
            "action": result["action"],
            "reason": result["reason"],
        })

    yield Event(output={
        **node_input,
        "assessments": assessments,
    })


# ===========================================================================
# Node 4 — report
# ===========================================================================
@node
async def report(ctx: Context, node_input: dict):
    logger.info("[shelter/report] Entry.")
    if node_input.get("error") == "no location set":
        yield Event(
            output={"error": "no location set"},
            content=types.Content(role="model", parts=[types.Part(text="no location set")]),
        )
        return

    assessments = node_input.get("assessments", [])
    category = node_input.get("category", 0)
    day_temp_min = node_input.get("day_temp_min", 0)
    date = node_input.get("date", "unknown")
    day_text = node_input.get("day_text", "today")
    plant_target = node_input.get("plant_target", "")
    category_name = CATEGORY_NAMES.get(category, str(category))

    if not assessments:
        if plant_target:
            text = f"No plants matching \"{plant_target}\" were found in your catalog."
        else:
            text = (
                f"You don't have any plants in your catalog yet, so there's nothing "
                f"to assess for {day_text} ({date}, {category_name}, low {day_temp_min:g}°C)."
            )
    else:
        lines = [
            f"Shelter Advisor for {day_text} ({date}): {category_name}, low of {day_temp_min:g}°C.\n"
        ]
        action_labels = {
            "move_indoors": "Move indoors",
            "move_outdoors": "Could move outdoors",
            "keep_as_is": "Keep as is",
        }
        for a in assessments:
            name = a["nickname"] or a["species"]
            label = action_labels.get(a["action"], a["action"])
            lines.append(f"- **{name}** ({a['species']}, currently {a['placement']}): {label}, {a['reason']}")
        lines.append(
            "\nVerify note: this is based on the forecast and each plant's stored "
            "tolerance, so always double-check the actual conditions and inspect "
            "your plants before moving them."
        )
        text = "\n".join(lines)

    yield Event(
        output={"assessments": assessments, "date": date, "category": category},
        content=types.Content(role="model", parts=[types.Part(text=text)]),
    )


# ===========================================================================
# Wire the graph
# ===========================================================================
shelter_advisor = Workflow(
    name="shelter_advisor",
    description=(
        "Deterministic Shelter Advisor. Given a day reference ('today', "
        "'tomorrow', or 'day after tomorrow') as the request text, fetches "
        "that day's forecast, categorizes its weather severity, and reports "
        "a move_indoors / move_outdoors / keep_as_is recommendation with a "
        "reason for every plant in the catalog, based on each plant's stored "
        "weather tolerance and current placement. Call this when the user "
        "asks whether they should move or shelter their plants for a "
        "specific day — do not try to answer that yourself."
    ),
    edges=[
        ("START", fetch_forecast),
        (fetch_forecast, categorize),
        (categorize, assess_plants),
        (assess_plants, report),
    ],
)
