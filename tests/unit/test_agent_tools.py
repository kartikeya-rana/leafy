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

import pytest
from datetime import datetime, timezone, timedelta
from app.agent import _parse_last_watered_text, update_plant_state
from app.storage import repository

@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    test_db = tmp_path / "test_leafy_agent.db"
    original_db_path = repository.DB_PATH
    repository.DB_PATH = str(test_db)
    repository.init_db()
    yield
    repository.DB_PATH = original_db_path

def test_parse_last_watered_text_valid():
    now = datetime.now(timezone.utc)
    # yesterday
    res = _parse_last_watered_text("yesterday")
    assert abs((res - (now - timedelta(days=1))).total_seconds()) < 10
    
    # today
    res = _parse_last_watered_text("today")
    assert abs((res - now).total_seconds()) < 10

    # 3 days ago
    res = _parse_last_watered_text("3 days ago")
    assert abs((res - (now - timedelta(days=3))).total_seconds()) < 10

    # ISO date (past)
    past_date_str = (now - timedelta(days=5)).strftime("%Y-%m-%d")
    res = _parse_last_watered_text(past_date_str)
    expected = datetime.strptime(past_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    assert res == expected

def test_parse_last_watered_text_future():
    now = datetime.now(timezone.utc)
    
    # tomorrow
    with pytest.raises(ValueError, match="invalid: future date"):
        _parse_last_watered_text("tomorrow")
        
    # in 3 days
    with pytest.raises(ValueError, match="invalid: future date"):
        _parse_last_watered_text("in 3 days")
        
    # future ISO date
    future_date_str = (now + timedelta(days=2)).strftime("%Y-%m-%d")
    with pytest.raises(ValueError, match="invalid: future date"):
        _parse_last_watered_text(future_date_str)

def test_update_plant_state_validation():
    # Add a plant first
    plant = repository.add_plant("local_user", "Rose", "Rosie", "indoor")
    
    # Update with valid date
    res = update_plant_state(plant.id, "indoor", "yesterday")
    assert res["status"] == "success"
    assert res["plant"]["last_watered_date"] is not None
    
    # Update with future date should fail
    res = update_plant_state(plant.id, "indoor", "tomorrow")
    assert res["status"] == "invalid"
    assert res["error"] == "invalid: future date"
    assert res["plant"] is None
    
    # Check that database was NOT updated with the future date (still yesterday's date)
    updated_plant = repository.get_plant(plant.id)
    now = datetime.now(timezone.utc)
    assert abs((updated_plant.last_watered_date - (now - timedelta(days=1))).total_seconds()) < 10
