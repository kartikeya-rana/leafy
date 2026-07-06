.PHONY: generate-traces grade eval

UV = /Users/kartikeyarana/Library/Python/3.13/bin/uv
AGENTS_CLI = /Users/kartikeyarana/.local/bin/agents-cli

generate-traces:
	PATH="/Users/kartikeyarana/Library/Python/3.13/bin:/Users/kartikeyarana/.local/bin:$$PATH" $(UV) run python tests/eval/generate_traces.py

grade:
	PATH="/Users/kartikeyarana/Library/Python/3.13/bin:/Users/kartikeyarana/.local/bin:$$PATH" $(UV) run python tests/eval/grade_traces.py

eval: generate-traces grade
	PATH="/Users/kartikeyarana/Library/Python/3.13/bin:/Users/kartikeyarana/.local/bin:$$PATH" $(UV) run pytest tests/eval/test_invariants.py
