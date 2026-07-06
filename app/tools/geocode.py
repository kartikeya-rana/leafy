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

import json
import logging
import re
import urllib.parse
import urllib.request
from typing import Optional
from pydantic import BaseModel

logger = logging.getLogger(__name__)

class GeocodeResult(BaseModel):
    found: bool
    lat: Optional[float] = None
    lon: Optional[float] = None
    resolved_name: Optional[str] = None
    country: Optional[str] = None
    message: Optional[str] = None

def _simplify_query(query: str) -> str:
    """Helper to simplify queries by removing digits/postal codes and excess spacing."""
    # Strip any digits/numbers (often representing postal codes or postal districts like 'Dublin 3')
    cleaned = re.sub(r'\d+', '', query)
    # Clean up spaces around commas
    cleaned = re.sub(r'\s*,\s*', ', ', cleaned)
    # Remove consecutive commas
    cleaned = re.sub(r',+', ',', cleaned)
    # Normalize multiple whitespace characters
    cleaned = re.sub(r'\s+', ' ', cleaned).strip(", ")
    return cleaned

def _query_api_raw(query: str) -> Optional[dict]:
    """Helper that actually queries the Open-Meteo Geocoding API."""
    url = f"https://geocoding-api.open-meteo.com/v1/search?name={urllib.parse.quote(query)}&count=1&language=en"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "LeafyBot/1.0 (Plant Care Assistant)"}
    )
    with urllib.request.urlopen(req, timeout=5) as response:
        if response.status == 200:
            return json.loads(response.read().decode("utf-8"))
    return None

def _query_api(query: str) -> Optional[dict]:
    from app.utils.resilient import run_resilient_sync, UnavailableResult
    res = run_resilient_sync(_query_api_raw, query)
    if isinstance(res, UnavailableResult):
        raise RuntimeError(res.message)
    return res

def geocode(location_text: str) -> GeocodeResult:
    """Converts a user's location text into latitude and longitude coordinates.

    Uses the Open-Meteo Geocoding API. If a query fails to match (e.g. because of
    postal code/district digits), it will clean and simplify the query and retry.

    Args:
        location_text: The location query (e.g., 'Dublin, Ireland', 'Berlin', 'Dublin 3, Ireland').

    Returns:
        A GeocodeResult containing coordinates and location metadata.
    """
    if not location_text or not location_text.strip():
        return GeocodeResult(found=False, message="Empty location query provided.")

    query = location_text.strip()

    def process_results(data: Optional[dict]) -> Optional[GeocodeResult]:
        if not data or "results" not in data or not data["results"]:
            return None
        result = data["results"][0]
        return GeocodeResult(
            found=True,
            lat=result.get("latitude"),
            lon=result.get("longitude"),
            resolved_name=result.get("name"),
            country=result.get("country"),
            message="Successfully resolved location coordinates."
        )

    # First attempt with original query
    try:
        data = _query_api(query)
        res = process_results(data)
        if res:
            return res
    except Exception as e:
        logger.warning(f"Geocoding attempt failed for '{query}': {e}")
        if "reach the geocoding service" in str(e):
            return GeocodeResult(found=False, message=str(e))

    # Fallback attempt with simplified query (e.g. stripping postal code numbers)
    simplified = _simplify_query(query)
    if simplified and simplified != query:
        logger.info(f"Retrying geocoding with simplified query: '{simplified}' (original: '{query}')")
        try:
            data = _query_api(simplified)
            res = process_results(data)
            if res:
                return res
        except Exception as e:
            logger.warning(f"Geocoding fallback attempt failed for '{simplified}': {e}")
            if "reach the geocoding service" in str(e):
                return GeocodeResult(found=False, message=str(e))

    # Fallback attempt treating unpunctuated multi-word queries as "City Country"
    # (e.g. 'Dublin Ireland' -> 'Dublin, Ireland'), since the geocoding API matches
    # on place name and doesn't infer comma placement itself.
    if "," not in simplified:
        words = simplified.split()
        if len(words) > 1:
            city_country = f"{' '.join(words[:-1])}, {words[-1]}"
            logger.info(f"Retrying geocoding as city/country split: '{city_country}'")
            try:
                data = _query_api(city_country)
                res = process_results(data)
                if res:
                    return res
            except Exception as e:
                logger.warning(f"Geocoding city/country split attempt failed for '{city_country}': {e}")
                if "reach the geocoding service" in str(e):
                    return GeocodeResult(found=False, message=str(e))

            # Also try just the first word alone (usually the city name).
            try:
                data = _query_api(words[0])
                res = process_results(data)
                if res:
                    return res
            except Exception as e:
                logger.warning(f"Geocoding first-word attempt failed for '{words[0]}': {e}")
                if "reach the geocoding service" in str(e):
                    return GeocodeResult(found=False, message=str(e))

    return GeocodeResult(
        found=False,
        message=f"Could not resolve coordinates for location: '{location_text}'."
    )
