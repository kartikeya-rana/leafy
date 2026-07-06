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

"""Regression test for the standalone weather MCP subprocess.

The weather server runs as a STANDALONE stdio subprocess whose sys.path does
NOT include the project root, so it cannot import the `app` package. This test
loads weather_server.py in isolation with the `app` package explicitly BLOCKED
from importing, then confirms get_weather returns a graceful fallback on a
mocked network failure — proving there is no `app` import and no raise
(no ModuleNotFoundError, no exception).
"""

import importlib.util
import os
import urllib.error
from unittest.mock import patch

import pytest

WEATHER_SERVER_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "mcp_servers", "weather_server.py"
)


class _BlockAppFinder:
    """Meta-path finder that makes any `import app` / `from app...` fail.

    Simulates the standalone subprocess environment where the project root is
    not on sys.path and the `app` package is therefore unavailable.
    """

    def find_spec(self, name, path=None, target=None):
        if name == "app" or name.startswith("app."):
            raise ModuleNotFoundError(f"No module named '{name}' (blocked for isolation test)")
        return None


def _load_weather_server_isolated():
    """Import weather_server.py fresh under a unique name with `app` blocked."""
    spec = importlib.util.spec_from_file_location(
        "weather_server_isolated", os.path.abspath(WEATHER_SERVER_PATH)
    )
    module = importlib.util.module_from_spec(spec)
    # exec_module runs the module's top-level code; if it imported `app` at
    # module scope, the blocker below would raise ModuleNotFoundError here.
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def isolated_weather_server():
    import sys

    blocker = _BlockAppFinder()
    sys.meta_path.insert(0, blocker)
    try:
        yield _load_weather_server_isolated()
    finally:
        sys.meta_path.remove(blocker)


def test_get_weather_isolated_returns_graceful_fallback(isolated_weather_server):
    mod = isolated_weather_server

    # Force the underlying HTTP fetch to fail transiently every attempt.
    err = urllib.error.URLError("network down")
    with patch.object(mod, "_fetch_weather_raw", side_effect=err) as mock_fetch:
        # Must NOT raise (no ModuleNotFoundError from a stray `app` import, and
        # no re-raise of the network failure).
        result = mod.get_weather(53.3498, -6.2603)

    # Graceful, valid WeatherResult-shaped fallback flagged unavailable.
    assert result is not None
    assert result.status == "unavailable"
    assert result.forecast == []
    assert result.recent_precip_mm_2d == 0.0
    assert "reach the weather service" in result.message
    # Retried on the transient error rather than giving up immediately.
    assert mock_fetch.call_count >= 2


def test_weather_server_module_has_no_app_import(isolated_weather_server):
    # Loading succeeded under the `app` blocker, which already proves there is
    # no top-level `app` import. Assert the module object is usable.
    mod = isolated_weather_server
    assert hasattr(mod, "get_weather")
    assert hasattr(mod, "WeatherResult")
