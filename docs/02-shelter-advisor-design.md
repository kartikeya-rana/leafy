# Capability 2 — Shelter Advisor (Design)

> This is the original design proposal, written before the build started. It is kept as a record of the design thinking, not as documentation of the shipped system. The submission track is Concierge Agents, not Agents for Good. The deterministic-graph approach described here did ship as designed. See the main [README](../README.md) for the current architecture.

_Status: original design proposal (superseded, see banner above)._

## 1. Goal
For a day the user asks about, tell them which plants to **move indoors/outdoors or leave as-is** — decided deterministically by comparing the day's weather against **each plant's own tolerance**. Preventive; barely any AI.

## 2. The core idea (AI for perception, code for the decision)
- **Weather → a category (perception).** Classify the day into an ordinal severity scale: `0 sunny · 1 cloudy · 2 rainy · 3 thunderstorm · 4 snow`. Approximate is fine.
- **Each plant carries its tolerance as data.** In the knowledge base, every plant has the **max weather category it tolerates outdoors** (+ optional `min_safe_temp_c` for frost).
- **The decision is a deterministic comparison.** If the day's category exceeds a plant's tolerance and it's outside → move it in. No per-plant AI reasoning, no hardcoded global thresholds.

This isolates the one fuzzy step (categorizing weather) from the policy (compare category vs tolerance), which makes the whole thing deterministic, unit-testable, and cheap.

## 3. Even the categorization can be deterministic
Open-Meteo returns a **WMO `weathercode`** per day. We map it to the 0-4 scale with a fixed lookup — so the categorization needs **no AI call**:

| Our category | WMO codes | Meaning |
|---|---|---|
| 0 sunny | 0, 1 | clear / mainly clear |
| 1 cloudy | 2, 3, 45, 48 | cloudy / fog |
| 2 rainy | 51-67, 80-82 | drizzle / rain / showers |
| 3 thunderstorm | 95-99 | thunderstorm |
| 4 snow | 71-77, 85-86 | snow |

(If we ever want the model to categorize instead, that's a drop-in fallback — but we don't need it.)

## 4. Per-plant tolerance data
Add to each plant in `data/plants.json`:

```json
"weather_tolerance": { "max_category": 2, "min_safe_temp_c": 10 }
```

- `max_category` (0-4): the harshest category the plant can safely stay outside in. (Basil ≈ 1, Rose ≈ 3, Succulent ≈ 1, Lavender ≈ 2, Ivy ≈ 3, Fern ≈ 1 — curated accurately.)
- `min_safe_temp_c` (optional): below this, recommend moving in even on a clear day (catches frost, which the severity scale alone misses).

**Unknown plants:** when a user adds a plant not in the KB, the model estimates its `weather_tolerance` **once, at add-time**, and we store it on the catalog item — every later shelter check then reuses it deterministically (one AI call ever, not one per check).

## 5. User journey
1. User asks about a specific day — "should I move my plants tomorrow?" / "it'll rain tomorrow, what do I do?" (today / tomorrow / day after).
2. Get that day's forecast (Weather MCP, now including `weathercode` + `temp_min`).
3. Map forecast → weather category (deterministic).
4. Loop all the user's plants; for each, compare category (+ temp) against its tolerance and current placement → action.
5. Report per-plant steps with reasons + a verify-yourself note.

## 6. Decision logic (pure function → unit-tested, quota-free)
For each plant, given `day_category`, `day_temp_min`, the plant's `weather_tolerance`, and its `placement`:
- **Outdoor** and (`day_category > max_category` **or** `day_temp_min < min_safe_temp_c`) → **move_indoors** (reason names the trigger).
- **Indoor** and `day_category == 0` and the plant is sun-loving/outdoor-suitable → **move_outdoors** (optional, "nice day to put it out").
- Otherwise → **keep_as_is**.

Every recommendation includes a reason and a verify note (Leafy's "guide, don't guarantee" principle). Leafy relays the deterministic result conversationally.

## 7. Architecture — the deterministic hybrid (revived)
Because the decision is deterministic with **no human-in-the-loop**, Shelter is implemented as a **small deterministic ADK graph** (`fetch_forecast → categorize → assess_plants → report`) that `Leafy` triggers — cleanly showcasing the **ADK graph Workflow API** without any resume complexity. (If we'd rather keep it a plain tool, that also works; the graph is chosen to demonstrate the Workflow API.)

## 8. Testing (almost entirely quota-free)
- Deterministic unit tests for: the WMO→category mapping, the day-selection helper, and the per-plant decision function (frosty clear night + outdoor basil → move_indoors; rainy day + outdoor rose (tol 3) → keep_as_is; snow + anything outdoor tender → move_indoors).
- The only AI touch is the one-time tolerance estimate for unknown plants — a tiny call, testable with a mock.

## 9. Build sub-steps
1. Add `weather_tolerance` to `data/plants.json` (curated) + extend the Weather MCP to return daily `weathercode` and `temp_min`.
2. `app/shelter/` pure logic: `categorize_weather(weathercode)`, `assess_plant(day_category, day_temp_min, tolerance, placement)` + unit tests.
3. Day-selection helper (today/tomorrow/day-after) + unit tests.
4. Deterministic ADK graph (`fetch_forecast → categorize → assess_plants → report`), triggered by Leafy; relay the report.
5. One-time model tolerance estimate for unknown plants at add-time (small, mocked in tests).

## 10. Open decisions for you
- Confirm the **0-4 scale** and the `weather_tolerance` schema (`max_category` + optional `min_safe_temp_c`).
- Include the optional **`min_safe_temp_c`** frost check, or keep strictly to the category scale for v1?
- Implement Shelter as a **deterministic ADK graph** (to also demonstrate the Workflow API), or a plain tool?
