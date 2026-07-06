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

import json
import urllib.error
from unittest.mock import MagicMock, patch
import pytest
from mcp_servers.weather_server import get_weather

def make_mock_response(status, data_dict):
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.read.return_value = json.dumps(data_dict).encode("utf-8")
    
    mock_context = MagicMock()
    mock_context.__enter__.return_value = mock_resp
    return mock_context

def test_get_weather_successful():
    mock_data = {
        "current": {
            "temperature_2m": 15.5,
            "relative_humidity_2m": 80.0,
            "wind_speed_10m": 12.5,
            "precipitation": 0.5
        },
        "daily": {
            "time": ["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-04", "2026-07-05"],
            "precipitation_sum": [1.0, 2.5, 0.0, 1.5, 3.0],
            "temperature_2m_max": [20.0, 21.0, 22.0, 23.0, 24.0],
            "temperature_2m_min": [10.0, 11.0, 12.0, 13.0, 14.0]
        }
    }
    
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = make_mock_response(200, mock_data)
        
        result = get_weather(latitude=53.3498, longitude=-6.2603)
        
        # Verify current weather parsing
        assert result.current.temp_c == 15.5
        assert result.current.humidity_pct == 80.0
        assert result.current.wind_kmh == 12.5
        assert result.current.precip_mm == 0.5
        
        # Verify 2-day recent precipitation calculation (sum of index 0 and 1: 1.0 + 2.5 = 3.5)
        assert result.recent_precip_mm_2d == 3.5
        
        # Verify forecast parsing (indices 2, 3, 4)
        assert len(result.forecast) == 3
        
        assert result.forecast[0].date == "2026-07-03"
        assert result.forecast[0].temp_max_c == 22.0
        assert result.forecast[0].temp_min_c == 12.0
        assert result.forecast[0].precip_mm == 0.0
        
        assert result.forecast[1].date == "2026-07-04"
        assert result.forecast[1].temp_max_c == 23.0
        assert result.forecast[1].temp_min_c == 13.0
        assert result.forecast[1].precip_mm == 1.5
        
        assert result.forecast[2].date == "2026-07-05"
        assert result.forecast[2].temp_max_c == 24.0
        assert result.forecast[2].temp_min_c == 14.0
        assert result.forecast[2].precip_mm == 3.0
        
        # Verify correct URL parameters
        args, _ = mock_urlopen.call_args
        called_url = args[0].full_url
        assert "latitude=53.3498" in called_url
        assert "longitude=-6.2603" in called_url
        assert "past_days=2" in called_url
        assert "forecast_days=3" in called_url

def test_get_weather_parses_daily_weathercode():
    mock_data = {
        "current": {
            "temperature_2m": 15.5,
            "relative_humidity_2m": 80.0,
            "wind_speed_10m": 12.5,
            "precipitation": 0.5
        },
        "daily": {
            "time": ["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-04", "2026-07-05"],
            "precipitation_sum": [1.0, 2.5, 0.0, 1.5, 3.0],
            "temperature_2m_max": [20.0, 21.0, 22.0, 23.0, 24.0],
            "temperature_2m_min": [10.0, 11.0, 12.0, 13.0, 14.0],
            "weathercode": [1, 2, 0, 61, 95]
        }
    }

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = make_mock_response(200, mock_data)

        result = get_weather(latitude=53.3498, longitude=-6.2603)

        # Forecast indices 2, 3, 4 -> weathercode 0, 61, 95
        assert result.forecast[0].weathercode == 0
        assert result.forecast[1].weathercode == 61
        assert result.forecast[2].weathercode == 95

        args, _ = mock_urlopen.call_args
        called_url = args[0].full_url
        assert "weathercode" in called_url


def test_get_weather_error_handling():
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.URLError("API failure")

        # On failure get_weather must RETURN a graceful fallback, never raise.
        result = get_weather(latitude=0.0, longitude=0.0)

        assert result.status == "unavailable"
        assert result.forecast == []
        assert result.recent_precip_mm_2d == 0.0
        assert "reach the weather service" in result.message
