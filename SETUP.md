# Running Leafy locally

Leafy is not hosted, so this guide takes you from a fresh clone to a working chat in a few minutes. Everything runs on your own machine with a single free API key. No other accounts, no cloud project, no payment.

If you only have five minutes, follow the Quick start and skip the rest.

---

## What you need first

You will need Python 3.11, 3.12, or 3.13 (Leafy requires `>=3.11,<3.14`), the [uv](https://docs.astral.sh/uv/getting-started/installation/) package manager, and a free Google AI Studio API key.

Install uv with one line:

- macOS or Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Windows (PowerShell): `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`

Get a free API key in under a minute at <https://aistudio.google.com/apikey>. This is the only credential you need, and it stays on your machine.

Leafy also calls Open-Meteo for geocoding and weather. That service is keyless and free, so there is nothing to configure for it.

---

## Quick start

Clone the project and add your key:

```bash
git clone <YOUR_REPO_URL> leafy
cd leafy
cp .env.example .env
```

Open `.env` and set it to the AI Studio path, so it contains exactly:

```
GEMINI_API_KEY=paste-your-key-here
```

Leave the Vertex AI lines commented out, so Leafy uses the simple key path rather than a cloud project. Then install and run:

```bash
uv sync
uv run python -m app.fast_api_app
```

Now open <http://localhost:8000/ui> in your browser. You will see the dashboard on the left and a chat on the right.

---

## Try it

The fastest way to see all three capabilities, in order:

1. Set your location. In chat, type `I live in Dublin` (any city works). Leafy needs a location before it can use live weather.
2. Add a plant. Type `add a basil`, then tell it whether the plant is indoors or outdoors and roughly when you last watered it. It confirms before saving.
3. Watering advice. Ask `when should I water my basil?` Leafy pulls live weather for your city and gives a watering window plus how to check the soil yourself.
4. Shelter advice. Ask `should I move any plants because of the weather?` It checks today's forecast against each plant and says keep, bring in, or put out.
5. Light and spot check. Upload a photo of a window or spot, tell it the direction the spot faces (for example `it faces south, about 170 degrees`), and ask `what can I grow here?` Sample photos and exact prompts are in [test-assets/](test-assets/).

Leafy is conversational, so you can ask these in any order and switch topics mid-conversation.

---

## Running the tests

Leafy is tested at three levels: deterministic unit tests on each tool, integration tests that lock in the behaviour of each user journey (which tools run, in what order, and with what preconditions), and an LLM-as-judge evaluation over full execution traces.

Unit and integration tests are the fast, deterministic safety net and need no API key:

```bash
uv run pytest tests/unit tests/integration
```

The trace-based evaluation generates fresh traces and grades them, so it needs your `.env` key and a working connection:

```bash
make eval
```

---

## Troubleshooting

If the page at `/ui` is blank, make sure you opened `http://localhost:8000/ui` with the `/ui`, not just the root. If your shell says `uv: command not found`, the uv install did not finish or you need to restart your terminal so it is on your PATH. If the model returns errors or empty replies, check that `.env` has `GEMINI_API_KEY=` with a real key and that the Vertex lines are commented out. If port 8000 is already in use, stop the other process or change the port on the last line of `app/fast_api_app.py`. If weather looks unavailable, Open-Meteo may be briefly rate-limiting; Leafy degrades gracefully and recovers on the next request.

---

## What is in the box

```
leafy/
├── app/                 # orchestrator agent, capabilities, security, storage, FastAPI + UI
├── mcp_servers/         # weather MCP server (FastMCP + Open-Meteo)
├── data/plants.json     # curated plant knowledge base
├── tests/               # unit / integration / eval
├── test-assets/         # sample images + prompts for judges to try it
├── docs/                # design docs and diagrams
├── .env.example         # copy to .env and add your key
└── SETUP.md             # this file
```
