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
import os
import re
import difflib
from typing import Optional
from pydantic import BaseModel, Field

class WateringProfile(BaseModel):
    baseline_interval_days: int
    min_days: int
    max_days: int
    drought_tolerance: str

class PlacementProfile(BaseModel):
    suitability: str
    notes: str

class LightProfile(BaseModel):
    need: str

class SoilProfile(BaseModel):
    preference: str

class WeatherTolerance(BaseModel):
    max_category: int = Field(
        description=(
            "Harshest daily weather category the plant safely tolerates outdoors "
            "for a day: 0 sunny, 1 cloudy, 2 rainy, 3 thunderstorm, 4 snow."
        )
    )
    min_safe_temp_c: int = Field(
        description="Lowest safe overnight/day-low temperature (Celsius) for the plant outdoors."
    )

class LightTier(BaseModel):
    min: int = Field(description="Lowest light tier (0-3) the plant thrives in.")
    max: int = Field(description="Highest light tier (0-3) the plant thrives in.")

class PlantCareProfile(BaseModel):
    id: str
    common_name: str
    scientific_name: str
    aliases: list[str] = Field(default_factory=list)
    watering: WateringProfile
    moisture_check: str
    placement: PlacementProfile
    light: LightProfile
    soil: SoilProfile
    weather_tolerance: WeatherTolerance
    light_tier: LightTier

class LookupResult(BaseModel):
    found: bool
    plant: Optional[PlantCareProfile] = None
    message: str

# In-memory cache for the plants data
_plants_cache: Optional[list[PlantCareProfile]] = None

def _load_plants() -> list[PlantCareProfile]:
    global _plants_cache
    if _plants_cache is not None:
        return _plants_cache

    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(os.path.dirname(current_dir))
    plants_json_path = os.path.join(project_root, "data", "plants.json")

    with open(plants_json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    _plants_cache = [PlantCareProfile(**p) for p in data.get("plants", [])]
    return _plants_cache

def _normalize(s: str) -> str:
    return re.sub(r'[^a-z0-9]', '', s.lower())

def list_all_plants() -> list[PlantCareProfile]:
    """Returns every plant care profile in the knowledge base."""
    try:
        return _load_plants()
    except Exception:
        return []

def plant_kb_lookup(query: str) -> LookupResult:
    """Look up a plant's care profile in the knowledge base by name, scientific name, or alias.

    Args:
        query: The name of the plant to look up (e.g., 'snake plant', 'sansevieria').

    Returns:
        A LookupResult containing the plant's care profile if found, or a not-found status.
    """
    if not query or not query.strip():
        return LookupResult(found=False, message="Empty query provided.")

    try:
        plants = _load_plants()
    except Exception as e:
        return LookupResult(found=False, message=f"Failed to load plant knowledge base: {e}")

    query_norm = _normalize(query)

    # 1. Try exact match on normalized names (common name, scientific name, aliases)
    for plant in plants:
        if _normalize(plant.common_name) == query_norm or _normalize(plant.scientific_name) == query_norm:
            return LookupResult(found=True, plant=plant, message=f"Found match: {plant.common_name}")
        for alias in plant.aliases:
            if _normalize(alias) == query_norm:
                return LookupResult(found=True, plant=plant, message=f"Found match via alias: {plant.common_name}")

    # 2. Try substring match (e.g., query is part of common name/scientific name/alias or vice-versa)
    if len(query_norm) > 2:
        for plant in plants:
            plant_names_norm = [_normalize(plant.common_name), _normalize(plant.scientific_name)] + [_normalize(a) for a in plant.aliases]
            for name_norm in plant_names_norm:
                if query_norm in name_norm or name_norm in query_norm:
                    return LookupResult(found=True, plant=plant, message=f"Found partial match: {plant.common_name}")

    # 3. Fuzzy match using difflib
    # Build a lookup mapping of all low-case names to the plant profiles
    name_to_plant = {}
    for plant in plants:
        name_to_plant[plant.common_name.lower()] = plant
        name_to_plant[plant.scientific_name.lower()] = plant
        for alias in plant.aliases:
            name_to_plant[alias.lower()] = plant

    possibilities = list(name_to_plant.keys())
    close_matches = difflib.get_close_matches(query.lower(), possibilities, n=1, cutoff=0.75)
    if close_matches:
        matched_name = close_matches[0]
        matched_plant = name_to_plant[matched_name]
        return LookupResult(found=True, plant=matched_plant, message=f"Found fuzzy match: {matched_plant.common_name} (matched '{matched_name}')")

    return LookupResult(found=False, message=f"Plant '{query}' not found in knowledge base.")


def resolve_care_profile(species: str) -> PlantCareProfile:
    """Look up a plant's care profile in the knowledge base by name, scientific name, or alias.
    If the species is not in the database, returns a generic care profile.
    """
    kb_res = plant_kb_lookup(species)
    if kb_res.found and kb_res.plant is not None:
        return kb_res.plant

    return PlantCareProfile(
        id="generic",
        common_name=species,
        scientific_name=species,
        aliases=[],
        watering=WateringProfile(
            baseline_interval_days=7,
            min_days=5,
            max_days=10,
            drought_tolerance="medium"
        ),
        moisture_check="Insert your finger about 2-3 inches (5 cm) into the soil to check if it feels dry before watering.",
        placement=PlacementProfile(
            suitability="both",
            notes="Can be kept indoors or outdoors depending on weather."
        ),
        light=LightProfile(need="low to bright direct sun"),
        soil=SoilProfile(preference="standard potting mix"),
        weather_tolerance=WeatherTolerance(
            max_category=1,
            min_safe_temp_c=10
        ),
        light_tier=LightTier(min=0, max=3)
    )
