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

import os
import pytest
from datetime import datetime, timezone
from app.storage import repository
from app.storage.repository import PlacementEnum

@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    # Set DB_PATH to a temporary file inside tmp_path for isolation
    test_db = tmp_path / "test_leafy.db"
    original_db_path = repository.DB_PATH
    repository.DB_PATH = str(test_db)
    
    # Initialize the test database
    repository.init_db()
    
    yield
    
    # Restore original path
    repository.DB_PATH = original_db_path

def test_profile_creation():
    user_id = "test_user_1"
    
    # Get or create (should create since it doesn't exist)
    profile = repository.get_or_create_profile(user_id)
    assert profile.user_id == user_id
    assert profile.location_text is None
    assert profile.lat is None
    assert profile.lon is None
    
    # Retrieve again (should get the existing one)
    profile2 = repository.get_or_create_profile(user_id)
    assert profile2.user_id == user_id
    assert profile2.created_at == profile.created_at

def test_update_location():
    user_id = "test_user_2"
    location_text = "Dublin, Ireland"
    lat = 53.3498
    lon = -6.2603
    resolved_name = "Dublin"
    
    profile = repository.update_location(user_id, location_text, lat, lon, resolved_name)
    assert profile.user_id == user_id
    assert profile.location_text == location_text
    assert profile.lat == lat
    assert profile.lon == lon
    assert profile.resolved_name == resolved_name

def test_add_plant():
    user_id = "test_user_3"
    species = "Snake Plant"
    nickname = "Slytherin"
    photo_path = "/path/to/photo.jpg"
    
    plant = repository.add_plant(
        user_id=user_id,
        species=species,
        nickname=nickname,
        photo_path=photo_path,
        placement=PlacementEnum.INDOOR
    )
    assert plant.id is not None
    assert plant.user_id == user_id
    assert plant.species == species
    assert plant.nickname == nickname
    assert plant.photo_path == photo_path
    assert plant.placement == PlacementEnum.INDOOR
    assert plant.last_watered_date is None

def test_list_plants():
    user_id = "test_user_4"
    repository.add_plant(user_id, "Fern", "Ferny", placement="indoor")
    repository.add_plant(user_id, "Cactus", "Prickly", placement="outdoor")
    
    plants = repository.list_plants(user_id)
    assert len(plants) == 2
    assert plants[0].species == "Cactus"  # Ordered by added_at DESC
    assert plants[1].species == "Fern"

def test_add_plant_ignores_weather_tolerance():
    user_id = "test_user_6"
    tolerance = {"max_category": 1, "min_safe_temp_c": 10}

    plant = repository.add_plant(
        user_id=user_id,
        species="Basil",
        placement="outdoor",
        weather_tolerance=tolerance,
    )
    # The Pydantic model no longer exposes weather_tolerance
    assert not hasattr(plant, "weather_tolerance")

    # Persisted correctly without weather_tolerance field on returned items
    fetched_list = repository.list_plants(user_id)
    assert not hasattr(fetched_list[0], "weather_tolerance")

    fetched_single = repository.get_plant(plant.id)
    assert not hasattr(fetched_single, "weather_tolerance")


def test_update_plant_state():
    user_id = "test_user_5"
    plant = repository.add_plant(user_id, "Aloe Vera", "Al", placement="indoor")
    assert plant.id is not None
    
    # Update state (both placement and last watered timestamp)
    new_time = datetime.now(timezone.utc)
    updated = repository.update_plant_state(
        plant_id=plant.id,
        placement=PlacementEnum.OUTDOOR,
        last_watered_date=new_time
    )
    
    assert updated is not None
    assert updated.placement == PlacementEnum.OUTDOOR
    # Assert time matches within 1 second tolerance due to DB string conversions
    assert abs((updated.last_watered_date - new_time).total_seconds()) < 1


def test_delete_plant():
    user_id = "test_user_delete"
    plant = repository.add_plant(user_id, "Aloe Vera", "Al", placement="indoor")
    assert plant.id is not None
    assert repository.get_plant(plant.id) is not None

    # Deleting existing plant -> returns True
    assert repository.delete_plant(plant.id) is True
    # The plant should be gone
    assert repository.get_plant(plant.id) is None

    # Deleting non-existent plant -> returns False, no error
    assert repository.delete_plant(99999) is False
