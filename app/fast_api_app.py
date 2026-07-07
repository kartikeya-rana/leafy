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

import contextlib
import logging
import os
from collections.abc import AsyncIterator

from a2a.server.tasks import InMemoryTaskStore
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from google.adk.cli.fast_api import get_fast_api_app
from google.adk.runners import Runner

from app.app_utils import services
from app.app_utils.a2a import attach_a2a_routes
from app.app_utils.typing import Feedback

load_dotenv()

# ---------------------------------------------------------------------------
# Cloud integrations degrade gracefully in local dev (no ADC credentials):
# telemetry and Cloud Logging are skipped, and feedback goes to stdlib logging.
# Cloud behaviour is unchanged when credentials are present.
# ---------------------------------------------------------------------------
_local_logger = logging.getLogger(__name__)


class _LocalFeedbackLogger:
    """Stand-in for google.cloud.logging's logger when no ADC is available."""

    def log_struct(self, payload: dict, severity: str = "INFO") -> None:
        _local_logger.log(
            logging.getLevelName(severity) if isinstance(severity, str) else severity,
            "feedback: %s",
            payload,
        )


try:
    from app.app_utils.telemetry import setup_telemetry

    setup_telemetry()
    import google.auth
    from google.cloud import logging as google_cloud_logging

    _, project_id = google.auth.default()
    logging_client = google_cloud_logging.Client()
    logger = logging_client.logger(__name__)
except Exception:  # pragma: no cover - exercised only without ADC
    _local_logger.warning(
        "Google Cloud credentials not found; telemetry and Cloud Logging "
        "disabled, running in local mode."
    )
    logger = _LocalFeedbackLogger()

allow_origins = (
    os.getenv("ALLOW_ORIGINS", "").split(",") if os.getenv("ALLOW_ORIGINS") else None
)

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "ui")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from app.agent import app as adk_app
    from app.agent import root_agent

    runner = Runner(
        app=adk_app,
        session_service=services.get_session_service(),
        artifact_service=services.get_artifact_service(),
        auto_create_session=True,
    )
    app.state.runner = runner
    app.state.agent_app_name = adk_app.name
    await attach_a2a_routes(
        app,
        agent=root_agent,
        runner=runner,
        task_store=InMemoryTaskStore(),
        rpc_path=f"/a2a/{adk_app.name}",
    )
    yield


app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    web=True,
    artifact_service_uri=services.ARTIFACT_SERVICE_URI,
    allow_origins=allow_origins,
    session_service_uri=services.SESSION_SERVICE_URI,
    otel_to_cloud=False,
    lifespan=lifespan,
)
app.title = "leafy"
app.description = "API for interacting with the Agent leafy"


@app.post("/feedback")
def collect_feedback(feedback: Feedback) -> dict[str, str]:
    """Collect and log feedback.

    Args:
        feedback: The feedback data to log

    Returns:
        Success message
    """
    logger.log_struct(feedback.model_dump(), severity="INFO")
    return {"status": "success"}


# ---------------------------------------------------------------------------
# Leafy web UI: read-only JSON APIs + static single-page app at /ui.
# Chat itself goes through the standard ADK endpoints (/run_sse and
# /apps/{app}/users/{user}/sessions), which get_fast_api_app already mounts.
# ---------------------------------------------------------------------------
@app.get("/api/plants")
def api_plants() -> dict:
    """The user's plant catalog, enriched with KB care facts for the UI."""
    from app.storage import repository
    from app.tools.plant_kb import resolve_care_profile

    plants = []
    for plant in repository.list_plants("local_user"):
        profile = resolve_care_profile(plant.species)
        is_generic = (profile.id == "generic")
        item = plant.model_dump(mode="json")
        item["care"] = {
            "scientific_name": "" if is_generic else profile.scientific_name,
            "light_need": profile.light.need,
            "light_tier": profile.light_tier.model_dump(),
            "baseline_interval_days": profile.watering.baseline_interval_days,
            "min_days": profile.watering.min_days,
            "max_days": profile.watering.max_days,
            "drought_tolerance": profile.watering.drought_tolerance,
            "weather_tolerance": profile.weather_tolerance.model_dump(),
        }
        plants.append(item)
    return {"plants": plants}


@app.delete("/api/plants/{plant_id}")
def api_delete_plant(plant_id: int) -> dict:
    """Deletes a plant from the catalog by its numeric ID."""
    from app.storage import repository
    from fastapi import HTTPException

    success = repository.delete_plant(plant_id)
    if not success:
        raise HTTPException(status_code=404, detail="Plant not found")
    return {"status": "success", "message": f"Plant {plant_id} deleted"}


@app.get("/api/profile")
def api_profile() -> dict:
    """The user's location display info only. Raw lat/lon is PII and is
    deliberately never exposed here."""
    from app.storage import repository

    profile = repository.get_or_create_profile("local_user")
    return {
        "resolved_name": profile.resolved_name,
        "location_text": profile.location_text,
    }


@app.get("/api/weather")
def api_weather() -> dict:
    """Gets the current weather and today's high/low for the user's location."""
    from app.storage import repository
    import urllib.request
    import urllib.parse
    import json

    profile = repository.get_or_create_profile("local_user")
    if not profile.lat or not profile.lon:
        return {"status": "no_location"}

    def map_wmo_code(code: int) -> str:
        if code == 0:
            return "Sunny"
        elif code in (1, 2, 3):
            return "Cloudy"
        elif code in (45, 48):
            return "Foggy"
        elif code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82):
            return "Rainy"
        elif code in (71, 73, 75, 77, 85, 86):
            return "Snowy"
        elif code in (95, 96, 99):
            return "Thunderstorm"
        return "Cloudy"

    try:
        params = {
            "latitude": profile.lat,
            "longitude": profile.lon,
            "current": "temperature_2m,weather_code",
            "daily": "temperature_2m_max,temperature_2m_min",
            "forecast_days": 1,
            "timezone": "auto"
        }
        query_string = urllib.parse.urlencode(params)
        url = f"https://api.open-meteo.com/v1/forecast?{query_string}"

        def _fetch_api_weather_raw() -> dict:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "LeafyBot/1.0 (Weather API)"}
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                if response.status == 200:
                    return json.loads(response.read().decode("utf-8"))
            raise Exception("Failed to fetch weather")

        from app.utils.resilient import run_resilient_sync, UnavailableResult
        data = run_resilient_sync(_fetch_api_weather_raw)
        if isinstance(data, UnavailableResult):
            return {"status": "error", "message": data.message}

        current_data = data.get("current", {})
        current_temp = current_data.get("temperature_2m", 0.0)
        weather_code = current_data.get("weather_code", 0)
        daily = data.get("daily", {})
        temp_max = daily.get("temperature_2m_max", [current_temp])[0]
        temp_min = daily.get("temperature_2m_min", [current_temp])[0]

        condition = map_wmo_code(weather_code)
        return {
            "status": "ok",
            "temp": current_temp,
            "condition": condition,
            "high": temp_max,
            "low": temp_min,
        }
    except Exception as e:
        return {"status": "error", "message": "I can't reach the weather service right now, please try again shortly."}


def _fetch_weather_data_raw(lat: float, lon: float) -> dict:
    import urllib.request
    import urllib.parse
    import json as _json

    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,weather_code,precipitation",
        "daily": "temperature_2m_max,temperature_2m_min,weather_code,precipitation_sum",
        "past_days": 2,
        "forecast_days": 1,
        "timezone": "auto",
    }
    url = f"https://api.open-meteo.com/v1/forecast?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "LeafyBot/1.0"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        if resp.status == 200:
            return _json.loads(resp.read().decode("utf-8"))
    raise Exception("Failed to fetch weather data")


def _fetch_weather_data(lat: float, lon: float) -> dict | None:
    """Shared helper: fetches current + daily weather from Open-Meteo. Returns
    parsed JSON dict or None on failure."""
    from app.utils.resilient import run_resilient_sync, UnavailableResult
    res = run_resilient_sync(_fetch_weather_data_raw, lat, lon)
    if isinstance(res, UnavailableResult):
        return None
    return res


def _map_wmo_code(code: int) -> str:
    if code == 0:
        return "Sunny"
    if code in (1, 2, 3):
        return "Cloudy"
    if code in (45, 48):
        return "Foggy"
    if code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82):
        return "Rainy"
    if code in (71, 73, 75, 77, 85, 86):
        return "Snowy"
    if code in (95, 96, 99):
        return "Thunderstorm"
    return "Cloudy"


@app.get("/api/dashboard")
def api_dashboard() -> dict:
    """Proactive 'today board': precomputed water + shelter status per plant,
    plus a summary. One Open-Meteo call, no LLM calls."""
    import math
    from datetime import datetime, timezone
    from app.storage import repository
    from app.tools.plant_kb import resolve_care_profile
    from app.shelter.rules import categorize_weather, assess_plant

    profile = repository.get_or_create_profile("local_user")
    plants_raw = repository.list_plants("local_user")

    # -- Weather (single fetch) ------------------------------------------------
    weather_info: dict | None = None
    day_category: int | None = None
    day_temp_min: float | None = None

    if profile.lat and profile.lon:
        data = _fetch_weather_data(profile.lat, profile.lon)
        if data:
            cur = data.get("current", {})
            daily = data.get("daily", {})
            cur_temp = cur.get("temperature_2m", 0.0)
            cur_code = cur.get("weather_code", 0)
            cur_precip = cur.get("precipitation", 0.0)
            # With past_days=2, the daily arrays are [2-days-ago, yesterday,
            # today]; today's forecast is the LAST entry, and the first two are
            # the recent history used for the rain adjustment.
            temp_max = daily.get("temperature_2m_max") or [cur_temp]
            temp_min = daily.get("temperature_2m_min") or [cur_temp]
            codes = daily.get("weather_code") or [cur_code]
            precip_sum = daily.get("precipitation_sum") or []
            t_max = temp_max[-1]
            t_min = temp_min[-1]
            daily_code = codes[-1]
            # Sum of the last 2 days before today (the historical entries).
            recent_precip_mm_2d = sum(precip_sum[:-1]) if len(precip_sum) > 1 else 0.0
            # Today's forecast rain total (the last daily entry).
            today_precip = precip_sum[-1] if precip_sum else 0.0

            weather_info = {
                "temp": cur_temp,
                "condition": _map_wmo_code(cur_code),
                "high": t_max,
                "low": t_min,
                "precip": cur_precip,
                "recent_precip_mm_2d": recent_precip_mm_2d,
                "today_precip": today_precip,
            }
            try:
                day_category = categorize_weather(daily_code)
                day_temp_min = t_min
            except ValueError:
                pass

    # -- Per-plant status ------------------------------------------------------
    now = datetime.now(timezone.utc)
    water_due_count = 0
    move_count = 0
    plant_items = []

    for plant in plants_raw:
        item = plant.model_dump(mode="json")

        # Care/reference data resolved dynamically
        care_profile = resolve_care_profile(plant.species)
        is_generic = (care_profile.id == "generic")
        item["care"] = {
            "scientific_name": "" if is_generic else care_profile.scientific_name,
            "light_need": care_profile.light.need,
            "baseline_interval_days": care_profile.watering.baseline_interval_days,
            "min_days": care_profile.watering.min_days,
            "max_days": care_profile.watering.max_days,
            "drought_tolerance": care_profile.watering.drought_tolerance,
        }

        # -- Water status --
        # Watering params come from the resolved care profile (unified source of truth)
        watering_params = item["care"]
        if plant.last_watered_date:
            weather_arg = None
            if weather_info:
                weather_arg = {
                    "current": {
                        "temp_c": weather_info["temp"],
                        "precip_mm": weather_info.get("precip", 0.0),
                    },
                    "recent_precip_mm_2d": weather_info.get("recent_precip_mm_2d", 0.0),
                    # Mirror the chat's forecast[0] (today) so both surfaces see
                    # the same "rain in the last 2 days (or forecast)" signal.
                    "forecast": [{"precip_mm": weather_info.get("today_precip", 0.0)}],
                }
            from app.watering.rules import compute_watering_window
            window_info = compute_watering_window(
                baseline_interval_days=watering_params["baseline_interval_days"],
                min_days=watering_params["min_days"],
                max_days=watering_params["max_days"],
                last_watered_date=plant.last_watered_date,
                weather=weather_arg,
                now=now,
                placement=plant.placement.value,
            )
            status = window_info["status"]
            days_until_due = window_info["days_until_due"]
            if status == "due":
                water_st = {"status": "due", "days_until_due": 0, "label": "Water today"}
                water_due_count += 1
            elif status == "soon":
                water_st = {"status": "soon", "days_until_due": days_until_due, "label": f"Water in {days_until_due} day{'s' if days_until_due != 1 else ''}"}
                water_due_count += 1
            else:
                days_since = (now - plant.last_watered_date).total_seconds() / 86400
                d_ago = max(0, math.floor(days_since))
                if d_ago == 0:
                    lbl = "Watered today"
                elif d_ago == 1:
                    lbl = "Watered yesterday"
                else:
                    lbl = f"Watered {d_ago} days ago"
                water_st = {"status": "ok", "days_until_due": days_until_due, "label": lbl}
        else:
            water_st = {"status": "unknown", "days_until_due": None, "label": "Unknown"}
        item["water"] = water_st

        # -- Shelter status --
        # assess_plant returns one of three actions: move_indoors (protective,
        # an outdoor plant needs to come in), move_outdoors (beneficial, an
        # indoor plant could enjoy a mild day within its tolerance), or
        # keep_as_is. Both move actions count toward "to move" so the summary
        # count matches the number of action chips shown on the cards.
        tolerance = care_profile.weather_tolerance.model_dump()
        if day_category is not None and day_temp_min is not None:
            try:
                assessment = assess_plant(
                    day_category=day_category,
                    day_temp_min=day_temp_min,
                    tolerance=tolerance,
                    placement=plant.placement.value,
                )
                action = assessment["action"]
                reason = assessment["reason"]
                if action == "move_indoors":
                    label = "Bring indoors"
                    move_count += 1
                elif action == "move_outdoors":
                    label = "Could go outside"
                    move_count += 1
                else:
                    label = "Fine where it is"
                item["shelter"] = {"action": action, "label": label, "reason": reason}
            except Exception:
                item["shelter"] = None
        else:
            item["shelter"] = None

        # Attention score for sorting (lower = more urgent)
        attn = 2  # default: no attention
        if water_st["status"] == "due":
            attn = 0
        elif water_st["status"] == "soon":
            attn = 0
        if item.get("shelter") and item["shelter"]["action"] in ("move_indoors", "move_outdoors"):
            attn = min(attn, 1)
        item["_attention"] = attn

        plant_items.append(item)

    # Sort: attention items first, then by name
    plant_items.sort(key=lambda p: (p["_attention"], (p.get("nickname") or p.get("species", "")).lower()))
    # Remove internal sort key
    for p in plant_items:
        del p["_attention"]

    return {
        "plants": plant_items,
        "summary": {
            "total_plants": len(plant_items),
            "water_due_count": water_due_count,
            "move_count": move_count,
            "weather": weather_info,
        },
        "location": profile.resolved_name or profile.location_text or None,
    }



if os.path.isdir(UI_DIR):
    app.mount("/ui", StaticFiles(directory=UI_DIR, html=True), name="leafy-ui")


# Main execution
if __name__ == "__main__":
    import uvicorn

    # Fixed default port 8000; override with PORT=... if it is already in use.
    # UI (dashboard + chat) is served at http://localhost:<port>/ui.
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
