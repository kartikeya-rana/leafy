.PHONY: run test generate-traces grade eval

# Tools are resolved from PATH so these targets work on any machine.
# Install uv from https://docs.astral.sh/uv/getting-started/installation/
UV = uv
AGENTS_CLI = agents-cli

# Canonical run command: starts the web server (dashboard + chat) on port 8000.
# Open http://localhost:8000/ui once it is up.
run:
	$(UV) run python -m app.fast_api_app

# Fast, deterministic test suite (no API key required).
test:
	$(UV) run pytest tests/unit tests/integration

generate-traces:
	$(UV) run python tests/eval/generate_traces.py

grade:
	$(UV) run python tests/eval/grade_traces.py

eval: generate-traces grade
	$(UV) run pytest tests/eval/test_invariants.py
