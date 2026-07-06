# Capability 3 — Spot / Light Check (Design)

> This is the original design proposal, written before the build started. It is kept as a record of the design thinking, not as documentation of the shipped system. The submission track is Concierge Agents, not Agents for Good. The orchestrator-does-vision approach described here did ship as designed. See the main [README](../README.md) for the current architecture.

_Status: original design proposal (superseded, see banner above)._

## 1. Goal
Help the user decide **which plants suit a particular spot** in their home — before they buy or move a plant there. The user shows a photo of the spot and which way it faces; Leafy estimates how much light that spot gets and recommends plants that match.

## 2. User journey (from Kartik)
1. The user uploads a **photo of the spot** (a windowsill, a corner, a patio).
2. They provide the **compass direction the spot faces** — ideally the precise azimuth from the phone compass (e.g. "103°, ESE"), and whether it's indoor (a window) or outdoor.
3. Leafy uses the photo + the direction + the user's **location** (latitude, from the saved profile) to estimate the spot's **light level**.
4. It recommends **which plants suit that spot** (from the knowledge base, and flags which of the user's own plants would/wouldn't do well there), with a note on how to verify.

## 3. Core idea — vision for perception, then a deterministic match
Same pattern that worked for Shelter: use the model for the fuzzy perception, then a deterministic rule for the recommendation.
- **Vision (perception):** Gemini reads the photo — is it a window or open outdoor area, how big, and how much sky/obstruction (buildings, trees, deep overhang).
- **Direction + latitude → a light tier (deterministic):** the compass azimuth maps to an aspect (roughly N / E / S / W), which — combined with hemisphere (sign of latitude) and Dublin's high-latitude, low-sun caveat — gives a baseline sunlight level. Obstructions (from vision) knock it down.
- **Match (deterministic):** compare the spot's estimated **light tier** against each plant's **light need**, and recommend the ones that fit.

Result: the only fuzzy step is reading the photo; the light estimate and the plant match are deterministic and unit-testable.

## 4. A light-tier scale (parallels weather_tolerance)
Add a `light_tier` to each plant in `data/plants.json`:

```json
"light_tier": { "min": 1, "max": 3 }
```

Scale: **0 = low/shade · 1 = medium indirect · 2 = bright indirect · 3 = direct sun.** A plant suits a spot if the spot's estimated tier falls within the plant's `[min, max]` (e.g. a snake plant tolerates 0-2; basil needs 2-3). Curated accurately from the existing `light.need` text.

## 5. Compass-azimuth → light heuristic (deterministic, unit-tested)
- Azimuth → aspect: N (315-45°), E (45-135°), S (135-225°), W (225-315°). *(Northern hemisphere: S faces the sun; flip for southern.)*
- Baseline tier by aspect (N hemisphere): **S** → 3 (most sun), **E/W** → 2 (half-day sun), **N** → 1 (little direct sun). East = gentle morning sun, West = harsher afternoon sun (noted in the reason).
- **Latitude/Dublin caveat:** at high latitude the sun is low and weak — cap outdoor tiers modestly and mention it.
- **Obstructions** (from the photo): heavy obstruction or a deep overhang subtracts a tier; a large unobstructed window adds confidence.
- **Indoor** windows are one tier lower than the same-facing outdoor spot (glass + depth into the room).

## 6. Architecture
The photo lives with the **orchestrator** (Gemini is multimodal and receives the uploaded image). Cleanest approach: the orchestrator does the vision read, then calls deterministic tools:
- `estimate_spot_light(azimuth_deg, indoor_or_outdoor, obstruction_level, latitude) -> {light_tier, reason}`
- `recommend_plants_for_light(light_tier) -> list of suitable plants (KB), and which of the user's catalog fit`

_(Passing an image into a sub-agent via `AgentTool` is awkward since it takes a string, so we keep vision in the orchestrator rather than adding a third LLM sub-agent. Optional alternative: the orchestrator extracts a structured light estimate and hands it to a `spot_planner` sub-agent — only if we want a third specialist for the multi-agent story.)_

## 7. Output
- The spot's estimated light ("bright indirect most of the day; west-facing, so strong afternoon sun") + confidence.
- Recommended plants for the spot (from the KB), and a check of the user's own catalog ("your basil would love it; your fern would scorch here").
- **Verify note:** light estimates from one photo are approximate — watch the spot across a day and count hours of direct sun.

## 8. Testing
- Deterministic unit tests (quota-free): the azimuth→aspect→tier heuristic (incl. hemisphere + indoor/outdoor + obstruction), and the light-tier plant match.
- Vision quality: validated manually in the playground and via the trajectory eval (needs quota).

## 9. Concept value
- Adds a **multimodal (vision)** capability — a strong, visual demo moment.
- Reuses the profile (latitude) and the plant KB.
- Deterministic light logic keeps it testable and cheap; only the photo read uses the model.

## 10. Build sub-steps
1. Add `light_tier` to `data/plants.json` (curated).
2. `app/spot/` pure logic: `estimate_spot_light(...)` + `recommend_plants_for_light(...)` + unit tests.
3. Wire as orchestrator tools; update Leafy's instruction to handle "what can I grow here / is this a good spot for X" — collect the photo + azimuth (+ indoor/outdoor), read the photo, call the tools, relay the recommendation with a verify note.
4. Manual playground test with a real spot photo; add a spot scenario to the trajectory eval.

## 11. Open decisions for you
- Confirm the **0-3 light-tier scale** and adding `light_tier` to `plants.json`.
- **Orchestrator-does-vision** (simplest, recommended) vs a third `spot_planner` sub-agent.
- Require the **precise azimuth in degrees**, or also accept a plain cardinal direction ("north-facing") when the user doesn't have the compass number?
