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
Mocked test for app/tools/tolerance_estimator.py — the one model call made
when adding a plant species that isn't in Leafy's KB. The model call itself
is mocked (no LLM, no quota); this verifies the ADK plumbing (Runner/session/
output_schema parsing) actually produces a usable dict.
"""
import json
from unittest.mock import patch

import pytest

from google.adk.models.llm_response import LlmResponse
from google.genai import types

from app.tools.tolerance_estimator import estimate_weather_tolerance


def _make_llm_response(max_category: int, min_safe_temp_c: int) -> LlmResponse:
    payload = json.dumps({"max_category": max_category, "min_safe_temp_c": min_safe_temp_c})
    return LlmResponse(
        content=types.Content(role="model", parts=[types.Part(text=payload)]),
        partial=False,
        turn_complete=True,
    )


@pytest.mark.asyncio
async def test_estimate_weather_tolerance_returns_mocked_estimate():
    async def fake_llm(*args, **kwargs):
        yield _make_llm_response(max_category=3, min_safe_temp_c=-2)

    with patch(
        "google.adk.models.google_llm.Gemini.generate_content_async",
        side_effect=fake_llm,
    ):
        result = await estimate_weather_tolerance("Rose")

    assert result == {"max_category": 3, "min_safe_temp_c": -2}


@pytest.mark.asyncio
async def test_estimate_weather_tolerance_falls_back_on_bad_output():
    async def fake_llm(*args, **kwargs):
        yield LlmResponse(
            content=types.Content(role="model", parts=[types.Part(text="not valid json")]),
            partial=False,
            turn_complete=True,
        )

    with patch(
        "google.adk.models.google_llm.Gemini.generate_content_async",
        side_effect=fake_llm,
    ):
        result = await estimate_weather_tolerance("Mystery Plant")

    # Falls back to the conservative default rather than raising.
    assert result == {"max_category": 1, "min_safe_temp_c": 10}
