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

import asyncio
import logging
from typing import AsyncGenerator
from google.adk.models.google_llm import Gemini
from google.adk.models.llm_response import LlmResponse
from google.adk.models.llm_request import LlmRequest
from google.adk import Event
from google.genai.types import Content, Part
from google.adk.runners import Runner

logger = logging.getLogger(__name__)

# 1. Monkeypatch Gemini.generate_content_async to retry on transient errors
original_generate_content_async = Gemini.generate_content_async

async def resilient_generate_content_async(
    self, llm_request: LlmRequest, stream: bool = False
) -> AsyncGenerator[LlmResponse, None]:
    from app.utils.resilient import is_transient_error, redact_pii

    max_attempts = 3
    attempt = 0
    delay = 0.5

    while attempt < max_attempts:
        attempt += 1
        buffer = []
        try:
            async def iterate_generator():
                async for item in original_generate_content_async(self, llm_request, stream=stream):
                    buffer.append(item)

            # Apply a 15-second timeout for the entire model call
            await asyncio.wait_for(iterate_generator(), timeout=15.0)

            for item in buffer:
                yield item
            return
        except Exception as e:
            if is_transient_error(e) and attempt < max_attempts:
                logger.warning(
                    f"Gemini API transient error on attempt {attempt}: {redact_pii(str(e))}. "
                    f"Retrying in {delay}s..."
                )
                await asyncio.sleep(delay)
                delay *= 2
            else:
                logger.error(
                    f"Gemini API hard failure or exhausted retries: {redact_pii(str(e))}"
                )
                raise RuntimeError("I'm briefly unavailable, please try again.")

Gemini.generate_content_async = resilient_generate_content_async


# 2. Monkeypatch Runner.run_async to catch all exceptions that reach the user centrally
original_run_async = Runner.run_async

async def central_run_async(self, *args, **kwargs) -> AsyncGenerator[Event, None]:
    from app.utils.resilient import redact_pii
    try:
        async for event in original_run_async(self, *args, **kwargs):
            yield event
    except Exception as e:
        logger.error(f"Central runner exception caught: {redact_pii(str(e))}", exc_info=e)

        friendly_msg = "I'm briefly unavailable, please try again."
        msg_lower = str(e).lower()
        if "weather" in msg_lower or "forecast" in msg_lower:
            friendly_msg = "I can't reach the weather service right now, please try again shortly."
        elif "geocode" in msg_lower:
            friendly_msg = "I can't reach the geocoding service right now, please try again shortly."

        yield Event(content=Content(role="model", parts=[Part(text=friendly_msg)]))

Runner.run_async = central_run_async
