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
import urllib.error
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from app.utils.resilient import (
    run_resilient_sync,
    run_resilient_async,
    UnavailableResult,
    is_transient_error,
    redact_pii
)
from app.tools.geocode import geocode
from mcp_servers.weather_server import get_weather
from google.adk import Event, Runner
from google.adk.agents import LlmAgent
from google.adk.models.google_llm import Gemini
from google.adk.models.llm_request import LlmRequest
from google.adk.sessions import InMemorySessionService
from google.adk.memory import InMemoryMemoryService


# --- Redaction Tests ---

def test_redact_pii():
    assert redact_pii("my email is test@example.com") == "my email is [REDACTED_EMAIL]"
    assert redact_pii("coords are 53.3498, -6.2603") == "coords are [REDACTED_COORDS]"
    assert redact_pii("http://test.com?lat=53.3498&lon=-6.2603") == "http://test.com?lat=[REDACTED_COORD]&lon=[REDACTED_COORD]"


# --- Transient Error Detection Tests ---

def test_is_transient_error():
    # urllib HTTPError 503 is transient
    e1 = urllib.error.HTTPError("http://test.com", 503, "Unavailable", None, None)
    assert is_transient_error(e1) is True

    # urllib HTTPError 404 is NOT transient
    e2 = urllib.error.HTTPError("http://test.com", 404, "Not Found", None, None)
    assert is_transient_error(e2) is False

    # TimeoutError is transient
    assert is_transient_error(TimeoutError("Timeout")) is True


# --- Resilient Wrapper Tests ---

def test_run_resilient_sync_success():
    func = MagicMock(return_value="success")
    res = run_resilient_sync(func, "arg", _initial_delay=0.01)
    assert res == "success"
    assert func.call_count == 1


def test_run_resilient_sync_transient_retry():
    # Fails twice with 503, succeeds on 3rd attempt
    err = urllib.error.HTTPError("http://test.com", 503, "Unavailable", None, None)
    func = MagicMock(side_effect=[err, err, "success"])
    
    res = run_resilient_sync(func, "arg", _initial_delay=0.01)
    assert res == "success"
    assert func.call_count == 3


def test_run_resilient_sync_hard_failure():
    err = urllib.error.HTTPError("http://test.com", 404, "Not Found", None, None)
    func = MagicMock(side_effect=err)
    
    res = run_resilient_sync(func, "arg", _initial_delay=0.01)
    assert isinstance(res, UnavailableResult)
    assert "unavailable" in res.message or "Not Found" in str(res.error)


@pytest.mark.asyncio
async def test_run_resilient_async_success():
    async_func = AsyncMock(return_value="success")
    res = await run_resilient_async(async_func, "arg", _initial_delay=0.01)
    assert res == "success"
    assert async_func.call_count == 1


@pytest.mark.asyncio
async def test_run_resilient_async_transient_retry():
    err = TimeoutError("Timeout")
    async_func = AsyncMock(side_effect=[err, "success"])
    
    res = await run_resilient_async(async_func, "arg", _initial_delay=0.01)
    assert res == "success"
    assert async_func.call_count == 2


# --- Dependency Mocking Tests ---

@patch("app.tools.geocode._query_api_raw")
def test_geocode_transient_retry(mock_query):
    # Mock geocode's inner query function to fail once transiently, then return a valid match
    err = urllib.error.HTTPError("http://test.com", 500, "Internal Server Error", None, None)
    valid_data = {
        "results": [{
            "latitude": 53.3498,
            "longitude": -6.2603,
            "name": "Dublin",
            "country": "Ireland"
        }]
    }
    mock_query.side_effect = [err, valid_data]

    res = geocode("Dublin")
    assert res.found is True
    assert res.lat == 53.3498
    assert mock_query.call_count == 2


@patch("app.tools.geocode._query_api_raw")
def test_geocode_hard_failure(mock_query):
    # Geocode fails repeatedly; must return GeocodeResult(found=False) with friendly message
    err = urllib.error.HTTPError("http://test.com", 503, "Service Unavailable", None, None)
    mock_query.side_effect = err

    res = geocode("Dublin")
    assert res.found is False
    assert "reach the geocoding service" in res.message


@patch("mcp_servers.weather_server._fetch_weather_raw")
def test_get_weather_transient_retry(mock_fetch):
    err = urllib.error.URLError("Connection refused")
    valid_data = {
        "current": {
            "temperature_2m": 15.0,
            "relative_humidity_2m": 70.0,
            "wind_speed_10m": 12.0,
            "precipitation": 0.0
        },
        "daily": {
            "time": ["2026-07-06"],
            "temperature_2m_max": [18.0],
            "temperature_2m_min": [10.0],
            "precipitation_sum": [0.0],
            "weathercode": [0]
        }
    }
    mock_fetch.side_effect = [err, valid_data]

    res = get_weather(53.3498, -6.2603)
    assert res.current.temp_c == 15.0
    assert mock_fetch.call_count == 2


@patch("mcp_servers.weather_server._fetch_weather_raw")
def test_get_weather_hard_failure(mock_fetch):
    err = urllib.error.HTTPError("http://test.com", 500, "Internal Server Error", None, None)
    mock_fetch.side_effect = err

    # The MCP tool must RETURN a graceful fallback, never raise.
    result = get_weather(53.3498, -6.2603)
    assert result.status == "unavailable"
    assert result.forecast == []
    assert "reach the weather service" in result.message


@pytest.mark.asyncio
async def test_gemini_transient_retry():
    # Test the monkeypatch resilient_generate_content_async
    model = Gemini()
    
    # Mock api_client
    mock_client = MagicMock()
    model.api_client = mock_client
    
    from google.genai.errors import ServerError
    err = ServerError(503, {}, None)
    
    # Success response
    mock_resp = MagicMock()
    mock_resp.text = "Hello!"
    
    # Stub the cached_property by writing directly to self.__dict__
    model.__dict__["api_client"] = mock_client
    
    mock_gen_content = AsyncMock(side_effect=[err, err, mock_resp])
    mock_client.aio.models.generate_content = mock_gen_content
    
    req = LlmRequest(model="gemini-2.5-flash", contents=[])
    
    from google.adk.models.llm_response import LlmResponse
    mock_llm_response = MagicMock(spec=LlmResponse)
    mock_llm_response.text = "Hello!"
    
    responses = []
    with patch("google.adk.models.llm_response.LlmResponse.create", return_value=mock_llm_response):
        async for resp in model.generate_content_async(req):
            responses.append(resp)
        
    assert len(responses) == 1
    assert responses[0].text == "Hello!"
    # 3 attempts = 1 initial + 2 retries
    assert mock_gen_content.call_count == 3


@pytest.mark.asyncio
async def test_runner_central_friendly_error():
    # Hard failure on model call -> runner must yield the friendly user message
    from google.genai.errors import ServerError
    err = ServerError(500, {}, None)
    
    model = Gemini()
    mock_client = MagicMock()
    mock_client.aio.models.generate_content.side_effect = err
    model.__dict__["api_client"] = mock_client
    
    # Create agent with this model
    agent = LlmAgent(name="test_agent", model=model)
    
    runner = Runner(
        app_name="test_app",
        agent=agent,
        session_service=InMemorySessionService(),
        memory_service=InMemoryMemoryService()
    )
    
    session = await runner.session_service.create_session(app_name="test_app", user_id="user")
    
    events = []
    from google.genai.types import Content, Part
    async for ev in runner.run_async(
        user_id="user",
        session_id=session.id,
        new_message=Content(role="user", parts=[Part(text="hello")])
    ):
        events.append(ev)

    assert len(events) >= 1
    text = events[-1].content.parts[0].text
    assert "briefly unavailable" in text
