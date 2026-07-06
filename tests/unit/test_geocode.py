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
import urllib.parse
from unittest.mock import MagicMock, patch
import pytest
from app.tools.geocode import geocode

def make_mock_response(status, data_dict):
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.read.return_value = json.dumps(data_dict).encode("utf-8")
    
    mock_context = MagicMock()
    mock_context.__enter__.return_value = mock_resp
    return mock_context

def test_geocode_successful_match():
    # Test a direct successful match for Dublin
    mock_data = {
        "results": [{
            "latitude": 53.3498,
            "longitude": -6.2603,
            "name": "Dublin",
            "country": "Ireland"
        }]
    }
    
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = make_mock_response(200, mock_data)
        
        result = geocode("Dublin, Ireland")
        
        assert result.found is True
        assert result.lat == 53.3498
        assert result.lon == -6.2603
        assert result.resolved_name == "Dublin"
        assert result.country == "Ireland"
        assert "Successfully resolved" in result.message
        
        # Verify the URL called was correct
        args, kwargs = mock_urlopen.call_args
        called_req = args[0]
        # urllib.request.Request is passed, extract full_url
        called_url = called_req.full_url
        assert "name=Dublin%2C%20Ireland" in called_url
        assert kwargs.get("timeout") == 5

def test_geocode_postal_district_fallback():
    # Test postal district fallback where "Dublin 3, Ireland" first returns empty,
    # then gets simplified to "Dublin, Ireland" and successfully resolves.
    first_attempt_data = {"results": []}
    second_attempt_data = {
        "results": [{
            "latitude": 53.3498,
            "longitude": -6.2603,
            "name": "Dublin",
            "country": "Ireland"
        }]
    }
    
    with patch("urllib.request.urlopen") as mock_urlopen:
        # We return empty for the first call, success for the second call
        mock_urlopen.side_effect = [
            make_mock_response(200, first_attempt_data),
            make_mock_response(200, second_attempt_data)
        ]
        
        result = geocode("Dublin 3, Ireland")
        
        assert result.found is True
        assert result.lat == 53.3498
        assert result.lon == -6.2603
        assert result.resolved_name == "Dublin"
        assert result.country == "Ireland"
        assert "Successfully resolved" in result.message
        
        # Verify urlopen was called twice
        assert mock_urlopen.call_count == 2
        
        # First call should have original query
        first_url = mock_urlopen.call_args_list[0][0][0].full_url
        assert "name=Dublin%203%2C%20Ireland" in first_url
        
        # Second call should have simplified query ("Dublin, Ireland")
        second_url = mock_urlopen.call_args_list[1][0][0].full_url
        assert "name=Dublin%2C%20Ireland" in second_url

def test_geocode_not_found():
    # Test case where both attempts return no results
    mock_empty_data = {"results": []}
    
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = [
            make_mock_response(200, mock_empty_data),
            make_mock_response(200, mock_empty_data)
        ]
        
        result = geocode("UnknownLocation 123")
        
        assert result.found is False
        assert result.lat is None
        assert result.lon is None
        assert "Could not resolve coordinates" in result.message
        
        # Should call twice: once for "UnknownLocation 123", once for simplified "UnknownLocation"
        assert mock_urlopen.call_count == 2

def test_geocode_network_failure():
    # Test network failure / timeout
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.URLError("Connection timed out")
        
        result = geocode("Paris, France")
        
        assert result.found is False
        assert result.lat is None
        assert result.lon is None
        assert "reach the geocoding service" in result.message
        
        # Since it raised an exception on the first attempt, it still tries fallback.
        # So it should call twice if the fallback query is different.
        # "Paris, France" simplified is "Paris, France" (same, so no fallback call).
        assert mock_urlopen.call_count == 1
