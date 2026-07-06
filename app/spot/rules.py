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
Pure, dependency-free decision logic for the Spot/Light Check capability.

No ADK / google-genai / DB imports here on purpose — deterministic,
unit-testable without any LLM, network, or database call. The orchestrator
(Leafy, in app/agent.py) is multimodal and reads the uploaded photo itself to
judge indoor/outdoor and obstruction level, then calls these as plain tools —
this module has no idea a photo was ever involved.
"""

from typing import Optional, TypedDict

# ---------------------------------------------------------------------------
# Light tiers
# ---------------------------------------------------------------------------
TIER_NAMES: dict[int, str] = {
    0: "low light / shade",
    1: "medium indirect light",
    2: "bright indirect light",
    3: "direct sun",
}

_MIN_TIER = 0
_MAX_TIER = 3


def _clamp_tier(value: int) -> int:
    return max(_MIN_TIER, min(_MAX_TIER, value))


# ---------------------------------------------------------------------------
# Direction parsing (accepts a precise azimuth or a cardinal direction)
# ---------------------------------------------------------------------------
_CARDINAL_AZIMUTHS: dict[str, float] = {
    "n": 0.0, "north": 0.0,
    "ne": 45.0, "northeast": 45.0, "north east": 45.0, "north-east": 45.0,
    "e": 90.0, "east": 90.0,
    "se": 135.0, "southeast": 135.0, "south east": 135.0, "south-east": 135.0,
    "s": 180.0, "south": 180.0,
    "sw": 225.0, "southwest": 225.0, "south west": 225.0, "south-west": 225.0,
    "w": 270.0, "west": 270.0,
    "nw": 315.0, "northwest": 315.0, "north west": 315.0, "north-west": 315.0,
}


def cardinal_to_azimuth(direction: str) -> float:
    """Converts a compass direction to an azimuth in degrees (0=N, 90=E,
    180=S, 270=W). Accepts either a cardinal direction ('south', 'SW') or a
    numeric azimuth string ('180', '202.5') — passed through as a float.

    Args:
        direction: A cardinal direction or a numeric azimuth string.

    Returns:
        Azimuth in degrees, normalized to [0, 360).

    Raises:
        ValueError: If direction can't be parsed as either.
    """
    text = (direction or "").strip().lower()
    if not text:
        raise ValueError("direction must not be empty.")

    if text in _CARDINAL_AZIMUTHS:
        return _CARDINAL_AZIMUTHS[text]

    try:
        return float(text) % 360.0
    except ValueError:
        raise ValueError(
            f"Could not parse direction {direction!r} as a cardinal direction "
            "(e.g. 'south', 'SW') or a numeric azimuth (e.g. '180')."
        )


def _azimuth_to_aspect(azimuth_deg: float) -> str:
    """Buckets an azimuth into one of the 4 cardinal aspects N/E/S/W using
    90-degree-wide sectors centered on each cardinal direction."""
    az = azimuth_deg % 360.0
    if az >= 315.0 or az < 45.0:
        return "N"
    if az < 135.0:
        return "E"
    if az < 225.0:
        return "S"
    return "W"


# ---------------------------------------------------------------------------
# estimate_spot_light
# ---------------------------------------------------------------------------
class SpotLightEstimate(TypedDict):
    light_tier: int
    reason: str


def estimate_spot_light(
    azimuth_deg: float,
    indoor_or_outdoor: str,
    obstruction_level: int,
    latitude: float,
) -> SpotLightEstimate:
    """Estimates a spot's light tier (0-3) from its facing direction, whether
    it's indoor/outdoor, how obstructed it is, and the location's latitude.

    Baseline (before adjustments), by aspect:
      Northern hemisphere: S=3, E/W=2, N=1
      Southern hemisphere: N=3, E/W=2, S=1  (mirrored — the sun tracks the
      opposite side of the sky south of the equator)

    Adjustments:
      - High latitude caps the achievable tier (lower sun angle / shorter,
        weaker daylight): |latitude| >= 66.5 caps at 1, >= 55 caps at 2.
      - obstruction_level (0 none, 1 partial, 2 heavy) subtracts that many
        tiers.
      - Indoor spots are one tier lower than the same-facing outdoor spot
        (a window always loses some intensity vs. being directly outside).
    Final tier is clamped to [0, 3].

    Args:
        azimuth_deg: Compass bearing the spot faces, in degrees (0=N, 90=E,
            180=S, 270=W). Use cardinal_to_azimuth() to convert a cardinal
            direction first if needed.
        indoor_or_outdoor: 'indoor' or 'outdoor'.
        obstruction_level: 0 (clear view of the sky), 1 (partially
            obstructed, e.g. a nearby tree or building), or 2 (heavily
            obstructed, e.g. deep shade most of the day).
        latitude: The location's latitude in degrees (-90..90).

    Returns:
        {'light_tier': int (0-3), 'reason': str}

    Raises:
        ValueError: If indoor_or_outdoor or obstruction_level is invalid.
    """
    if indoor_or_outdoor not in ("indoor", "outdoor"):
        raise ValueError(f"indoor_or_outdoor must be 'indoor' or 'outdoor', got {indoor_or_outdoor!r}")
    if obstruction_level not in (0, 1, 2):
        raise ValueError(f"obstruction_level must be 0, 1, or 2, got {obstruction_level!r}")

    aspect = _azimuth_to_aspect(azimuth_deg)
    northern = latitude >= 0

    if northern:
        baseline_by_aspect = {"S": 3, "E": 2, "W": 2, "N": 1}
    else:
        baseline_by_aspect = {"N": 3, "E": 2, "W": 2, "S": 1}
    baseline = baseline_by_aspect[aspect]

    abs_lat = abs(latitude)
    if abs_lat >= 66.5:
        lat_cap = 1
    elif abs_lat >= 55.0:
        lat_cap = 2
    else:
        lat_cap = 3
    capped = min(baseline, lat_cap)

    after_obstruction = capped - obstruction_level
    after_indoor = after_obstruction - (1 if indoor_or_outdoor == "indoor" else 0)
    final_tier = _clamp_tier(after_indoor)

    hemisphere = "northern" if northern else "southern"
    reason_parts = [
        f"{aspect}-facing (azimuth {azimuth_deg:g}°) in the {hemisphere} hemisphere "
        f"gives a baseline of tier {baseline} ({TIER_NAMES[baseline]})."
    ]
    if lat_cap < baseline:
        reason_parts.append(
            f"Latitude {latitude:g}° is high enough to cap this at tier {lat_cap}."
        )
    if obstruction_level > 0:
        reason_parts.append(
            f"Obstruction level {obstruction_level} reduces it by {obstruction_level} tier(s)."
        )
    if indoor_or_outdoor == "indoor":
        reason_parts.append("It's indoors, so it's one tier lower than the same spot outside.")
    reason_parts.append(f"Estimated light tier: {final_tier} ({TIER_NAMES[final_tier]}).")

    return {"light_tier": final_tier, "reason": " ".join(reason_parts)}


# ---------------------------------------------------------------------------
# recommend_plants_for_light
# ---------------------------------------------------------------------------
class PlantLightTier(TypedDict):
    min: int
    max: int


class KbPlantLightInfo(TypedDict):
    common_name: str
    light_tier: PlantLightTier


class CatalogPlantLightInfo(TypedDict):
    id: Optional[int]
    species: str
    nickname: Optional[str]
    light_tier: Optional[PlantLightTier]


def _fits(light_tier: int, tolerance: PlantLightTier) -> bool:
    return tolerance["min"] <= light_tier <= tolerance["max"]


def recommend_plants_for_light(
    light_tier: int,
    kb_plants: list[KbPlantLightInfo],
    catalog_plants: list[CatalogPlantLightInfo],
) -> dict:
    """Recommends KB plants for a given spot's light tier, and classifies
    each of the user's own catalog plants as fitting or struggling there.

    Args:
        light_tier: The spot's estimated light tier, 0-3 (see estimate_spot_light).
        kb_plants: Knowledge-base plants, each with 'common_name' and
            'light_tier' ({'min': int, 'max': int}).
        catalog_plants: The user's own plants, each with 'id', 'species',
            'nickname', and 'light_tier' ({'min': int, 'max': int} if known,
            else None for a species with no light data).

    Returns:
        {
          'recommended': [{'common_name': str}, ...],  # KB plants that fit
          'catalog_fit': [
              {'id', 'species', 'nickname', 'fits': True|False|None, 'reason': str}
              for each catalog plant, in the same order given
          ],
        }
    """
    recommended = [
        {"common_name": p["common_name"]}
        for p in kb_plants
        if _fits(light_tier, p["light_tier"])
    ]

    catalog_fit = []
    for p in catalog_plants:
        tolerance = p.get("light_tier")
        name = p.get("nickname") or p["species"]
        if tolerance is None:
            catalog_fit.append({
                "id": p.get("id"),
                "species": p["species"],
                "nickname": p.get("nickname"),
                "fits": None,
                "reason": f"No light data for {name} ({p['species']}), so guidance would be generic.",
            })
            continue

        min_desc = TIER_NAMES.get(tolerance["min"], f"tier {tolerance['min']}")
        max_desc = TIER_NAMES.get(tolerance["max"], f"tier {tolerance['max']}")
        tier_desc = TIER_NAMES.get(light_tier, f"tier {light_tier}")
        range_desc = min_desc if tolerance["min"] == tolerance["max"] else f"{min_desc} to {max_desc}"

        if _fits(light_tier, tolerance):
            reason = (
                f"{name} thrives in {range_desc}, "
                f"and this spot has {tier_desc}, a good fit."
            )
            fits = True
        else:
            direction = "too little" if light_tier < tolerance["min"] else "too much"
            reason = (
                f"{name} needs {range_desc}, "
                f"and this spot has {tier_desc} ({direction} light), so it would struggle here."
            )
            fits = False

        catalog_fit.append({
            "id": p.get("id"),
            "species": p["species"],
            "nickname": p.get("nickname"),
            "fits": fits,
            "reason": reason,
        })

    return {"recommended": recommended, "catalog_fit": catalog_fit}
