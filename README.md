# 🌿 Leafy — a personal plant-care concierge

Leafy is an AI concierge for your houseplants. Tell it where you live and what you own once, and from then on it tells you when to water, when to move a plant out of bad weather, and what will actually thrive in a given spot — tuned to your local climate and each plant's real needs.

Track: Concierge Agents
Built for the Kaggle × Google 5-Day AI Agents Intensive Capstone.

---

## The story

Houseplants are everywhere. About two in three US households own at least one, in a market worth roughly $20 billion a year, and for a lot of people it is the small daily ritual that makes a home feel calmer and more alive.

Most of those plants quietly struggle. In one survey of 2,000 millennials, the average person had already killed seven houseplants, and two thirds called themselves a "plant murderer." The reason they gave, most often, was simply not knowing how much light or water the plant actually needed, and improper watering, overwatering above all, is widely cited as the single biggest killer. The advice people follow tends to make it worse. "Water once a week, give it bright light" ignores the two things that actually decide a plant's fate, which are your local climate and where you place it. In a cloudy, rainy city a fixed weekly schedule drowns a succulent, and a "bright" north-facing windowsill barely keeps basil going.

Getting it right is harder than it sounds, because it is not a lookup. A good call depends on several live, changing things at once: the plant's baseline needs, today's temperature and rainfall, whether it sits indoors or outdoors, and how long since it was last watered. And the apps that exist are reactive. You snap a photo and get told the plant is already sick, which is a diagnosis after the damage is done, and single-photo diagnosis is unreliable anyway.

So the question is simple. What if you had someone who already knew your plants, your home, and today's weather, and told you what to do before anything went wrong?

That is what Leafy is. It is a concierge, not a diagnosis tool. You give it your location and your plants once, and after that it holds that context and does the reasoning for you. A dashboard surfaces what needs attention today, and in chat you can ask about watering, sheltering from the weather, or what suits a new spot, in any order. Leafy gathers what it needs, reasons over live weather and each plant's profile, and explains itself.

The payoff is that the help is preventive instead of reactive, and personal instead of generic. Every recommendation comes with a way to check it yourself, for example "push a finger three centimetres into the soil and only water if it is dry," so Leafy guides and you stay in control. It never claims a certainty it does not have.

## Why an agent, and not a chatbot or a database

A database could store your plants, and a classifier could name one from a photo. Neither can look at your specific plant, in your climate, on today's date, and decide what to do. That takes an agent: something that gathers the right information, some from you and some from live weather, reasons across it, and explains the call. Leafy earns the label by coordinating specialist tools and a sub-agent under one orchestrator that decides, turn by turn, what the situation needs. The decisions that have to be trustworthy are computed in deterministic code rather than left to the model, which is what keeps a conversational assistant from drifting into a confident guess.

## What it does today

Three capabilities are built end to end and share the same foundations of location, plant catalogue, and live weather.

| Capability | Status | What it does |
|---|---|---|
| Watering Advisor | Built | Recommends when to water next from live weather, the plant's needs, the last-watered date, and indoor or outdoor placement, with a plant-specific way to check the soil yourself. |
| Shelter Advisor | Built | Reads today's forecast and, per plant, recommends keeping it put, bringing it in, or moving it out, with the reason. |
| Light / Spot Check | Built | From a photo of a spot plus the compass direction it faces, estimates the light it gets and which plants would suit it. |
| Soil Matcher | Roadmap | Recommends a soil or mix for a plant from the soils you already own. |

## How it works

Leafy is orchestrated by an LLM. One orchestrator on Gemini holds a toolbox and decides, from what you actually ask, which tools to call and when to ask for more. For the parts that must be correct and consistent, such as a watering window, a move-indoors call, or a light rating, it hands off to deterministic code or a grounded specialist, so the model phrases the answer but does not invent it. Perceive with the model, decide with code is the throughline of the whole design.

```mermaid
flowchart TD
    You([You]) <--> UI[Dashboard + chat]
    UI --> Guard["Security screen<br/>prompt-injection · PII redaction · image checks"]
    Guard --> Leafy["🌿 Leafy orchestrator · Gemini<br/>routes the request, gathers data, delegates"]

    %% Data-access tools the orchestrator owns
    Leafy --> CAT["catalogue + profile tools"]
    Leafy --> KBT["plant knowledge lookup"]
    Leafy --> GEO["geocode"]
    Leafy --> MCP["Weather MCP server<br/>separate process"]
    CAT --> DB[("SQLite<br/>plants · placement · dates · location")]
    KBT --> KB[("plants.json<br/>care profiles · tolerances · light needs")]
    GEO --> OM1["Open-Meteo geocoding"]
    MCP --> OM2["Open-Meteo forecast"]

    %% Capabilities
    Leafy --> WR["watering_reasoner · sub-agent (LLM)<br/>fed plant + care profile + live weather<br/>→ watering window + moisture check"]
    Leafy --> SH["Shelter advisor · deterministic graph<br/>forecast → severity 0–4, vs each plant's<br/>tolerance + placement → keep / in / out"]
    Leafy --> SP["Spot / Light Check · vision + deterministic<br/>direction + latitude + obstruction → light tier 0–3<br/>matched to each plant's light range"]

    %% What the self-contained deterministic capabilities read for themselves
    SH -. reads .-> DB
    SH -. reads .-> KB
    SH -. reads .-> MCP
    SP -. reads .-> DB
    SP -. reads .-> KB
```

The orchestrator owns the data-access tools. It reads your catalogue and profile from SQLite, looks up care profiles, tolerances, and light needs from the plant knowledge base, geocodes a city, and fetches live weather from the Weather MCP server. Each capability then uses that data in one of two ways.

Watering advice is gather-then-delegate. The orchestrator collects the plant record, its care profile, and the live forecast, and passes all three to the watering specialist, a sub-agent that reasons over them and returns a structured window plus a moisture check. The specialist never reads the stores itself; it only sees what the orchestrator hands it.

Shelter and spot advice are self-contained deterministic modules. The shelter advisor reads your plants and their placement from SQLite and each plant's tolerance from the knowledge base, fetches the day's forecast from the Weather MCP, maps that forecast to a severity from 0 (sunny) to 4 (snow) using the weather code, and compares it against every plant's tolerance and current placement to decide keep, move indoors, or move outdoors, each with a reason. The spot and light check reads your latitude from the profile and plant light needs from the knowledge base; from the compass direction the spot faces, your latitude, and how obstructed it is, it computes a light tier from 0 (shade) to 3 (direct sun) and matches that against each plant's light range. In both, the only fuzzy step is perception, such as reading a photo; every decision is computed in plain code and unit-tested.

## Design decisions

A conversational assistant benefits from the model choosing the path, so Leafy uses LLM orchestration rather than a wired state machine, which would feel like a scripted chatbot that cannot change topic. Where a capability genuinely needs guaranteed steps, it drops into a small deterministic sub-flow, which is why the Shelter advisor is computed rather than reasoned.

The watering specialist is invoked as a tool and returns its result, so the orchestrator stays in control of the conversation instead of handing it away. Weather is exposed as a standalone server over the Model Context Protocol, so it is reusable and language-agnostic rather than a private in-process function. The plant catalogue and profile live in SQLite as the single source of truth, and care details are resolved from a plant's identity at the point of use rather than copied onto records, so nothing goes stale. Security runs as a callback before the model ever sees a turn.

## Course concepts demonstrated

| Concept | Where it lives |
|---|---|
| Multi-agent system (ADK) | Leafy orchestrator plus the watering specialist sub-agent, with meaningful tool use |
| MCP server | `mcp_servers/weather_server.py`, a FastMCP weather server connected over MCP |
| Security and guardrails | `app/security/`, prompt-injection defence, PII redaction, and image validation as a before-model callback |
| Evaluation | Trajectory tests plus deterministic invariants and LLM-as-judge scoring over execution traces |
| Agent skills (agents-cli) | Built with the agents-cli toolchain and its local eval |

## Tech stack

Google ADK, Gemini 2.5 Flash, the agents-cli toolchain, MCP via FastMCP, Open-Meteo for keyless geocoding and weather, SQLite, Pydantic, FastAPI for the app and UI, and pytest. Built with agentic coding tools including Antigravity.

## Run it and test it

Everything runs locally on a single free API key. See [SETUP.md](SETUP.md) for the few-minute setup, the prompts that exercise all three capabilities, and the test commands. Sample photos and prompts for judges to try are in [test-assets/](test-assets/).

## How it is tested

Leafy is tested at three levels. Deterministic unit tests cover each tool in isolation, including the security screen. Integration tests lock in the behaviour of each user journey, asserting which tools run, in what order, and with what preconditions, so a fix in one place cannot silently break another. On top of that, an LLM-as-judge evaluation scores full execution traces. Because the model now chooses the path, the tests check the steps taken, not only the final answer.

## Responsible AI

Leafy is built to guide, not to guarantee. Every recommendation is paired with a way to verify it by hand, and it flags when its guidance is generic, for example for a plant that is not in the knowledge base. It does not fabricate data. If it cannot resolve a location or find a plant, it says so rather than guessing. It also stays in character and does not expose its internals.

## Limitations and roadmap

The current build is single-user with a local profile, and multi-user accounts are future work. Identifying a plant from a photo, with a confirmation step, is designed as an enhancement to the add-a-plant flow. The Soil Matcher is designed and shares the same foundations. Deployment to a managed runtime, with a proactive daily trigger, is on the roadmap. The security screen checks each incoming turn but does not yet scrub already-stored conversation history.

## Sources for the numbers above

- Houseplant ownership: [New York Botanical Garden](https://www.nybg.org/planttalk/if-youre-a-plant-person-youre-not-alone/), citing National Gardening Association data
- Market size: [Data Bridge Market Research](https://www.databridgemarketresearch.com/reports/global-indoor-plants-market)
- Millennials killing an average of seven plants and calling themselves plant murderers: [Deseret News](https://www.deseret.com/u-s-world/2020/1/27/21083948/millennials-pressures-plant-parenting-survey-parenthood-houseplant/) and [SWNS](https://swns-research.medium.com/seven-in-10-millennials-consider-themselves-plant-parents-11ef0b34773c), reporting a 2020 OnePoll survey
- Overwatering as the leading cause of houseplant death: [University of Connecticut extension](https://soiltesting.cahnr.uconn.edu/watering-houseplants/)

These are survey and market-research figures, not peer-reviewed studies, and are used here as industry-standard context for the problem, not as scientific claims.

---

Plant care data here is curated guidance, not a guarantee. Always check your plant and soil before watering.
