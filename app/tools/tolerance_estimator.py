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
One-shot weather-tolerance estimation for plant species not in Leafy's KB.

Used by the add_plant flow: when plant_kb_lookup doesn't find a species, this
makes exactly one model call to estimate a reasonable outdoor weather
tolerance, so the Shelter Advisor's deterministic rules (app/shelter/rules.py)
still have something to work with for generic/unknown plants. The estimate is
stored once on the catalog item at add-time — never re-estimated afterward.
"""

import logging

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.tools.plant_kb import WeatherTolerance

logger = logging.getLogger("leafy_agent")

# Conservative fallback if the model call fails or returns something unparsable.
_DEFAULT_TOLERANCE: dict = {"max_category": 1, "min_safe_temp_c": 10}

_estimator_agent = LlmAgent(
    name="tolerance_estimator",
    model="gemini-2.5-flash",
    instruction="""You estimate an outdoor weather tolerance for a plant species that
isn't in Leafy's knowledge base, so Leafy can later decide whether that plant
needs to be brought indoors on a given day.

Given a plant species name, estimate:
- max_category: the harshest daily weather category it could safely tolerate
  outdoors for a day, on a scale of 0-4 (0 sunny, 1 cloudy, 2 rainy,
  3 thunderstorm, 4 snow).
- min_safe_temp_c: the lowest safe overnight/day-low temperature in Celsius
  it could tolerate outdoors.

Base this on typical botanical knowledge of the species (or its closest
relative if unfamiliar). Be conservative for delicate/tropical/houseplant-like
species and more generous for known-hardy garden/outdoor species.
""",
    output_schema=WeatherTolerance,
)


async def estimate_weather_tolerance(species: str) -> dict:
    """Makes one model call to estimate a plant species' outdoor weather tolerance.

    Args:
        species: The plant species or common name to estimate tolerance for.

    Returns:
        dict with 'max_category' (int, 0-4) and 'min_safe_temp_c' (int). Falls
        back to a conservative default if the model call fails.
    """
    runner = Runner(
        app_name="tolerance_estimator",
        agent=_estimator_agent,
        session_service=InMemorySessionService(),
    )
    try:
        session = await runner.session_service.create_session(
            app_name="tolerance_estimator", user_id="internal"
        )
        content = types.Content(role="user", parts=[types.Part(text=species)])

        result: dict | None = None
        async for event in runner.run_async(
            user_id="internal", session_id=session.id, new_message=content
        ):
            if not event.content or not event.content.parts:
                continue
            for part in event.content.parts:
                if part.text:
                    try:
                        result = WeatherTolerance.model_validate_json(part.text).model_dump()
                    except Exception:
                        pass

        if result is None:
            logger.warning(
                f"[tolerance_estimator] No usable estimate for '{species}'; using default."
            )
            return dict(_DEFAULT_TOLERANCE)
        return result
    except Exception as e:
        logger.error(f"[tolerance_estimator] Estimation failed for '{species}': {e}")
        return dict(_DEFAULT_TOLERANCE)
    finally:
        await runner.close()
