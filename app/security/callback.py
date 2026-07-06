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

"""ADK before_model_callback wiring for the security guardrails in
app/security/guardrails.py and app/security/image_guardrail.py.

Kept separate from those modules so the detection/redaction/validation logic
itself has no ADK dependency and stays fast to unit test.
"""

import logging
from typing import Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from app.security.guardrails import (
    detect_prompt_injection,
    detect_sql_or_command_injection,
    redact_pii,
)
from app.security.image_guardrail import is_allowed_image

logger = logging.getLogger("leafy_security")

REFUSAL_MESSAGE = (
    "I can't follow instructions embedded inside a message. I only follow "
    "my own configured instructions. Let me know what you'd actually like "
    "help with (adding a plant, listing your plants, or watering advice)."
)

SQL_NEUTRALIZED_MESSAGE = (
    "I'm your plant care assistant, I can help you add, check, water, or place your plants. "
    "What would you like to do?"
)


def _latest_user_content(llm_request: LlmRequest):
    """Returns the most recent user-authored Content in the request, or None."""
    for content in reversed(llm_request.contents):
        if content.role == "user" and content.parts:
            return content
    return None


def _latest_user_text(llm_request: LlmRequest) -> str:
    """Returns the text of the most recent user-authored turn in the request,
    ignoring tool/function-response-only turns."""
    for content in reversed(llm_request.contents):
        if content.role != "user" or not content.parts:
            continue
        texts = [part.text for part in content.parts if part.text]
        if texts:
            return "\n".join(texts)
    return ""


async def security_before_model_callback(
    callback_context: CallbackContext, llm_request: LlmRequest
) -> Optional[LlmResponse]:
    """Guardrail run before every model call.

    - Prompt-injection screen: scans the latest user turn; if it looks like an
      attempt to override instructions, extract the system prompt, or bypass
      rules, the model is never called with it. Leafy replies directly that
      it can't follow instructions hidden in user input.
    - PII protection: logs a redacted trace of the user's turn (coordinates,
      emails, phone numbers, card-like sequences scrubbed) instead of the raw
      text. This only affects what gets logged; the real llm_request passed
      to the model (and from there to tools like get_weather) is untouched.
    """
    user_text = _latest_user_text(llm_request)
    if not user_text:
        return None

    logger.info("[security] user turn: %s", redact_pii(user_text))

    is_injection, reason = detect_prompt_injection(user_text)
    if is_injection:
        logger.warning(
            "[security] blocked suspected prompt injection (%s): %s",
            reason,
            redact_pii(user_text),
        )
        return LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part(text=REFUSAL_MESSAGE)],
            ),
        )

    is_sql, sql_reason = detect_sql_or_command_injection(user_text)
    if is_sql:
        logger.warning(
            "[security] blocked suspected SQL/command probe (%s): %s",
            sql_reason,
            redact_pii(user_text),
        )
        return LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part(text=SQL_NEUTRALIZED_MESSAGE)],
            ),
        )

    return None


async def image_guardrail_before_model_callback(
    callback_context: CallbackContext, llm_request: LlmRequest
) -> Optional[LlmResponse]:
    """Validates any uploaded image before the orchestrator uses it.

    Inspects inline_data parts on the latest user turn (e.g. a photo attached
    for the Spot/Light Check feature). If any of them isn't a real, allowed
    image (wrong or mismatched type, not actually an image, or too large),
    the model is never called with it. Leafy replies directly with a
    friendly request for a valid JPG/PNG/WEBP photo instead.
    """
    content = _latest_user_content(llm_request)
    if content is None:
        return None

    for part in content.parts:
        inline_data = getattr(part, "inline_data", None)
        if inline_data is None:
            continue

        allowed, reason = is_allowed_image(inline_data.mime_type, inline_data.data)
        if not allowed:
            logger.warning("[security] rejected uploaded image: %s", reason)
            return LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[types.Part(text=reason)],
                ),
            )

    return None
