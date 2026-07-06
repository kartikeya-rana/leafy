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
import http.client
import inspect
import json
import logging
import re
import socket
import time
import urllib.error
import urllib.parse
from typing import Callable, Any, TypeVar, Optional

logger = logging.getLogger(__name__)

T = TypeVar("T")

class UnavailableResult:
    """Typed result indicating that a service is unavailable."""
    def __init__(self, message: str, error: Optional[Exception] = None):
        self.message = message
        self.error = error
        self.status = "unavailable"

    def __repr__(self) -> str:
        return f"UnavailableResult(message={self.message!r})"

    def __str__(self) -> str:
        return self.message

    def get(self, key: str, default: Any = None) -> Any:
        if key == "status":
            return "unavailable"
        if key == "message":
            return self.message
        return default


def redact_pii(text: str) -> str:
    """Redacts coordinates, emails, phone numbers, and potential sensitive values from logs."""
    if not isinstance(text, str):
        text = str(text)
    # Redact email addresses
    text = re.sub(r'[\w\.-]+@[\w\.-]+\.\w+', '[REDACTED_EMAIL]', text)
    # Redact coordinates (lat/lon) with 3+ decimal places: e.g. 53.3498, -6.2603
    text = re.sub(r'-?\d+\.\d{3,},\s*-?\d+\.\d{3,}', '[REDACTED_COORDS]', text)
    # Redact individual latitude/longitude query params in URLs
    text = re.sub(r'(latitude|longitude|lat|lon)=[-?]?\d+\.\d+', r'\1=[REDACTED_COORD]', text)
    return text


def is_transient_error(e: Exception) -> bool:
    """Returns True if the exception represents a transient/retryable network/API failure."""
    if isinstance(e, urllib.error.HTTPError):
        # HTTP 5xx codes are transient
        return 500 <= e.code < 600
    if isinstance(e, urllib.error.URLError):
        # Timeouts or connection errors
        return True
    if isinstance(e, (socket.timeout, TimeoutError, asyncio.TimeoutError)):
        return True
    if isinstance(e, (ConnectionError, ConnectionRefusedError, ConnectionResetError)):
        return True
    if isinstance(e, http.client.HTTPException):
        return True

    # Check google.genai or other model/API client exceptions
    err_name = e.__class__.__name__
    if err_name in ("ServerError", "APIError"):
        status_code = getattr(e, "code", getattr(e, "status_code", None))
        if status_code in (503, 429) or (status_code and 500 <= status_code < 600):
            return True
        msg = str(e).upper()
        if "UNAVAILABLE" in msg or "RESOURCE_EXHAUSTED" in msg or "RATE" in msg or "DEMAND" in msg:
            return True

    msg = str(e).upper()
    if "503" in msg or "UNAVAILABLE" in msg or "EXPERIENCING HIGH DEMAND" in msg or "TIMEOUT" in msg or "RATE" in msg:
        return True

    return False


async def run_resilient_async(
    func: Callable[..., Any],
    *args: Any,
    _timeout: float = 5.0,
    _max_attempts: int = 3,
    _initial_delay: float = 0.5,
    **kwargs: Any
) -> Any:
    """Runs an async or sync function with a timeout, retries on transient errors, and returns UnavailableResult on failure."""
    redacted_args = [redact_pii(str(arg)) for arg in args]
    redacted_kwargs = {k: redact_pii(str(v)) for k, v in kwargs.items()}

    attempt = 0
    delay = _initial_delay

    while attempt < _max_attempts:
        attempt += 1
        try:
            if inspect.iscoroutinefunction(func):
                res = await asyncio.wait_for(func(*args, **kwargs), timeout=_timeout)
                return res
            else:
                loop = asyncio.get_running_loop()
                def sync_wrapper():
                    return func(*args, **kwargs)
                res = await asyncio.wait_for(
                    loop.run_in_executor(None, sync_wrapper),
                    timeout=_timeout
                )
                return res
        except Exception as e:
            func_name = getattr(func, "__name__", str(func))
            if is_transient_error(e) and attempt < _max_attempts:
                logger.warning(
                    f"Transient error on attempt {attempt} running {func_name} "
                    f"with args {redacted_args}, kwargs {redacted_kwargs}: {redact_pii(str(e))}. "
                    f"Retrying in {delay}s..."
                )
                await asyncio.sleep(delay)
                delay *= 2
            else:
                logger.error(
                    f"Hard failure or exhausted retries running {func_name} "
                    f"with args {redacted_args}, kwargs {redacted_kwargs}: {redact_pii(str(e))}"
                )
                friendly_msg = "I'm briefly unavailable, please try again."
                func_name_lower = func_name.lower()
                if "weather" in func_name_lower or "forecast" in func_name_lower:
                    friendly_msg = "I can't reach the weather service right now, please try again shortly."
                elif "geocode" in func_name_lower or "query_api" in func_name_lower:
                    friendly_msg = "I can't reach the geocoding service right now, please try again shortly."
                return UnavailableResult(friendly_msg, error=e)


def run_resilient_sync(
    func: Callable[..., Any],
    *args: Any,
    _timeout: float = 5.0,
    _max_attempts: int = 3,
    _initial_delay: float = 0.5,
    **kwargs: Any
) -> Any:
    """Synchronous version of run_resilient_async."""
    redacted_args = [redact_pii(str(arg)) for arg in args]
    redacted_kwargs = {k: redact_pii(str(v)) for k, v in kwargs.items()}

    attempt = 0
    delay = _initial_delay

    while attempt < _max_attempts:
        attempt += 1
        try:
            return func(*args, **kwargs)
        except Exception as e:
            func_name = getattr(func, "__name__", str(func))
            if is_transient_error(e) and attempt < _max_attempts:
                logger.warning(
                    f"Transient error on attempt {attempt} running {func_name} "
                    f"(sync): {redact_pii(str(e))}. Retrying in {delay}s..."
                )
                time.sleep(delay)
                delay *= 2
            else:
                logger.error(
                    f"Hard failure or exhausted retries running {func_name} "
                    f"(sync): {redact_pii(str(e))}"
                )
                friendly_msg = "I'm briefly unavailable, please try again."
                func_name_lower = func_name.lower()
                if "weather" in func_name_lower or "forecast" in func_name_lower:
                    friendly_msg = "I can't reach the weather service right now, please try again shortly."
                elif "geocode" in func_name_lower or "query_api" in func_name_lower:
                    friendly_msg = "I can't reach the geocoding service right now, please try again shortly."
                return UnavailableResult(friendly_msg, error=e)
