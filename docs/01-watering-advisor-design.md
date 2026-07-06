# Capability 1 — Watering Advisor (Design)

> This is the original design proposal, written before the build started. It is kept as a record of the design thinking, not as documentation of the shipped system. Two things below changed during the build: the submission track is Concierge Agents, not Agents for Good, and the final architecture uses one LLM orchestrator that delegates to a watering specialist through `AgentTool`, rather than the ADK graph described in section 5. See the main [README](../README.md) for what actually shipped.

_Status: original design proposal (superseded, see banner above)._

## 1. Goal
Help a plant owner know **when to water next**, tuned to their **local weather** and the **plant's own needs**, and always tell them **how to verify it themselves**. Leafy guides; the human confirms.

## 2. User journey
1. **First visit / onboarding.** The user gives their home location once (e.g. "Dublin 3, Ireland"). Leafy resolves it to coordinates and saves it to the user's profile. This location is reused by every capability (weather, sun, etc.).
2. **Build the catalog.** The user adds a plant by uploading a photo. Leafy identifies the species, **shows its best guess and asks the user to confirm** (or correct/type it). On confirm, the plant is saved to the user's personal catalog with its care profile pulled from Leafy's plant database.
3. **Ask for watering help.** The user selects an existing plant from the catalog (or adds a new one), then provides two things: **when they last watered it** and whether it's currently **indoor or outdoor** (this placement is per-plant state that can change day to day).
4. **Leafy reasons.** It fetches current/recent weather for the saved location, combines that with the plant's baseline needs and the placement/last-watered state, and returns a recommendation.
5. **Recommendation.** "Water in ~2 days (around Sat 5 Jul). Why: cool, humid, low evaporation in Dublin this week, and *Snake Plant* is drought-tolerant. **Check yourself:** push a finger ~3 cm into the soil — only water if it's dry at that depth."

## 3. Functional requirements
**User provides:** home location (once); a plant photo when adding a plant; plant selection from catalog; last-watered date; indoor/outdoor placement.

**Leafy fetches/derives automatically:** coordinates from the location; plant species from the photo; the plant's baseline care profile from its database; current + recent weather (temperature, humidity, wind, recent rainfall).

**Factors in the recommendation:** plant's baseline watering interval and drought tolerance; days since last watered; temperature; humidity; wind (evaporation); indoor vs outdoor; **recent rainfall** (if outdoor and it rained, delay).

**Output contains:** a next-watering window (relative + a date), a one-line "why", and a **plant-specific manual moisture check**. Never a hard guarantee — it's guidance to verify.

**Edge cases:** plant not in database → fall back to the model's general knowledge, flagged as generic/lower-confidence; it rained and plant is outdoors → recommend delaying; identification uncertain → ask the user to confirm or type the name.

## 4. Shared foundations this slice establishes (reused later)
- **Location service** — text → coordinates, saved to a user profile.
- **Plant knowledge base** — a bundled JSON of common plants with per-plant care attributes.
- **Personal catalog + persistence** — the user's plants and their mutable state (placement, last-watered).
- **Weather MCP server** — the live-weather tool (also used by Shelter Advisor later).
- **Security pattern** and **evaluation harness** — set up once, reused by every capability.

## 5. Agent design (ADK 2.0 graph)
An orchestrator routes the request to the Watering Advisor path. Within it, the graph flows through function nodes with human-in-the-loop (`RequestInput`) where the user must decide something:

- **ensure_location** — if the profile has no location, ask for it → `geocode` tool → save. _(HITL if missing.)_
- **select_or_add_plant** —
  - _Add:_ `identify_plant` (Gemini vision) → propose species → **`RequestInput` confirm** → `plant_kb_lookup` → save to catalog.
  - _Select:_ load the chosen plant from the catalog.
- **collect_state** — ask **last-watered date** + **indoor/outdoor** → save state. _(HITL.)_
- **security_screen** — redact location coordinates (PII) and screen any free-text the user typed for prompt-injection, before the model reasons.
- **fetch_weather** — `get_weather(lat, lon)` via the Weather MCP.
- **watering_reasoner** (LLM) — combine plant profile + weather + state → next-watering window + plant-specific moisture-check text.

### Tools
- `geocode(location_text) -> {lat, lon, resolved_name}`
- `get_weather(lat, lon) -> {temp, humidity, wind, recent_precip, short_forecast}` _(Weather MCP)_
- `plant_kb_lookup(species) -> care_profile`
- `identify_plant(image) -> species_candidates` _(Gemini vision)_
- catalog/profile read + write _(data-access module)_

### Data model (initial schemas)
- **UserProfile:** `location_text, lat, lon, resolved_name, created_at`
- **PlantCatalogItem:** `id, species (kb_id or free text), nickname, photo_path, placement (indoor|outdoor), last_watered_date, added_at`
- **PlantKB entry:** `id, common_name, scientific_name, aliases[], watering{base_interval_days, drought_tolerance}, moisture_check_method, indoor_outdoor_suitability` _(light/soil fields stubbed for later capabilities)_

## 6. Backend decisions (made per your delegation — override any)
- **Geocoding = Open-Meteo Geocoding API**, not Google Maps. Reason: free, **no API key**, and we already use Open-Meteo for weather — one dependency, zero billing setup. (Google Maps needs a key + billing for the same result here.)
- **Weather = Open-Meteo**, wrapped as our **MCP server** (this is the MCP rubric concept).
- **Persistence = SQLite** locally via a small data-access module (swappable to Firestore if we deploy stateful later). Simple, file-based, zero setup.
- **Plant identification = Gemini vision + user confirmation.** Single-photo ID is not 100% reliable (the same concern you raised about disease apps), so we never act on it silently — the user confirms.
- **Recommendation = agent reasons over tool outputs** (weather + plant profile + state) rather than a hard-coded formula — this keeps it genuinely agentic and explainable.

## 7. Responsible-AI thread ("guide + verify")
Every output pairs a recommendation with a way to check it manually, and flags uncertainty (generic profile, unsure ID). This differentiates Leafy from one-shot "diagnosis" apps and anchors the Responsible-AI story in the writeup.

## 8. Security touchpoints (rubric: Security)
- **Location is PII.** Coordinates are stored but **kept out of the LLM prompt and logs** — the weather tool consumes them and returns only weather; the reasoner sees weather, not the address.
- **Prompt-injection screen** on any free text the user types (e.g. a plant nickname) before the model reasons.

## 9. Build sub-steps (each becomes an Antigravity prompt)
1. Plant knowledge base JSON (~20 common plants) + `plant_kb_lookup` tool.
2. Data-access module + SQLite (UserProfile + PlantCatalogItem).
3. `geocode` tool (Open-Meteo) + ensure_location flow.
4. Weather MCP server (Open-Meteo) + `get_weather` tool.
5. `identify_plant` (Gemini vision) + confirm (RequestInput).
6. watering_reasoner node + assemble the graph end-to-end.
7. security_screen node (PII redaction + injection).
8. Test in playground.
9. Evaluation harness (LLM-as-judge: watering-correctness + security-containment).
10. Deploy to Agent Runtime.

## 10. Open decisions for you
- Confirm **Open-Meteo geocoding** (vs. Google Maps) — I recommend Open-Meteo.
- Confirm **slice-1 scope includes onboarding + catalog** (location save, photo-ID, catalog), or trim to a leaner first slice.
- Any must-have plants for the starter database (I'll seed ~20 common houseplants + a few Irish-garden-friendly ones otherwise).
