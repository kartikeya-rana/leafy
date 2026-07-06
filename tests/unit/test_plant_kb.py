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

from app.tools.plant_kb import plant_kb_lookup

def test_plant_kb_lookup_direct_hit():
    # Direct common name hit (exact match)
    result = plant_kb_lookup("Snake Plant")
    assert result.found is True
    assert result.plant is not None
    assert result.plant.id == "snake_plant"
    assert result.plant.common_name == "Snake Plant"

    # Case-insensitive direct hit
    result = plant_kb_lookup("snake plant")
    assert result.found is True
    assert result.plant is not None
    assert result.plant.id == "snake_plant"

def test_plant_kb_lookup_alias_hit():
    # Alias hit (exact match)
    result = plant_kb_lookup("Sansevieria")
    assert result.found is True
    assert result.plant is not None
    assert result.plant.id == "snake_plant"

    # Case-insensitive alias hit
    result = plant_kb_lookup("sansevieria")
    assert result.found is True
    assert result.plant is not None
    assert result.plant.id == "snake_plant"

def test_plant_kb_lookup_not_found():
    # Not found case
    result = plant_kb_lookup("Kryptonite Plant")
    assert result.found is False
    assert result.plant is None
    assert "not found" in result.message.lower()

    # Empty query case
    result = plant_kb_lookup("   ")
    assert result.found is False
    assert result.plant is None
