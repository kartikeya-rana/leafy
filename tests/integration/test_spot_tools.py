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

"""
Integration test for the Spot/Light Check tool wrappers in app/agent.py
(estimate_spot_light, recommend_plants_for_light). No LLM anywhere in these
tools, so this exercises real DB + KB wiring directly (no mocking needed).
"""
import os
import tempfile

import pytest

_tmp_dir = tempfile.mkdtemp()
_test_db = os.path.join(_tmp_dir, "leafy_spot_test.db")

import app.storage.repository as _repo_module
_repo_module.DB_PATH = _test_db


def test_estimate_spot_light_never_assumes_a_default_location():
    """Location journey lock: with no saved location, the spot tool must NOT
    silently assume a default (e.g. Dublin). It reports 'no location set' so
    the orchestrator asks the user for their city first, per the requirement
    'never assumes a default'."""
    _repo_module.init_db()
    conn = _repo_module.get_db_connection()
    conn.execute("DELETE FROM plant_catalog;")
    conn.execute("DELETE FROM user_profiles;")
    conn.commit()
    conn.close()

    from app.agent import estimate_spot_light

    result = estimate_spot_light(direction="south", indoor_or_outdoor="outdoor", obstruction_level=0)
    # No location saved -> explicit "no location set" signal, not a guessed default.
    assert result.get("error") == "no location set"
    assert "no location set" in result["reason"].lower()


def test_estimate_spot_light_uses_saved_profile_latitude():
    _repo_module.init_db()
    conn = _repo_module.get_db_connection()
    conn.execute("DELETE FROM plant_catalog;")
    conn.execute("DELETE FROM user_profiles;")
    conn.commit()
    conn.close()

    _repo_module.update_location(
        user_id="local_user", location_text="Tromso, Norway", lat=69.6, lon=18.9, resolved_name="Tromso"
    )

    from app.agent import estimate_spot_light

    # South-facing would normally be tier 3, but Tromso's high latitude caps it at 1.
    result = estimate_spot_light(direction="south", indoor_or_outdoor="outdoor", obstruction_level=0)
    assert result["light_tier"] == 1
    assert "69.6" in result["reason"]


def test_recommend_plants_for_light_wires_kb_and_catalog():
    _repo_module.init_db()
    conn = _repo_module.get_db_connection()
    conn.execute("DELETE FROM plant_catalog;")
    conn.execute("DELETE FROM user_profiles;")
    conn.commit()
    conn.close()

    # Boston Fern (KB light_tier 1-1) and Lavender (KB light_tier 3-3).
    _repo_module.add_plant(user_id="local_user", species="Boston Fern", placement="indoor")
    _repo_module.add_plant(user_id="local_user", species="Lavender", placement="outdoor")

    from app.agent import recommend_plants_for_light

    result = recommend_plants_for_light(light_tier=1)

    recommended_names = {p["common_name"] for p in result["recommended"]}
    assert "Boston Fern" in recommended_names
    assert "Lavender" not in recommended_names

    by_species = {p["species"]: p for p in result["catalog_fit"]}
    assert by_species["Boston Fern"]["fits"] is True
    assert by_species["Lavender"]["fits"] is False
    # Lavender needs tier 3 (full sun); tier 1 is too little light for it.
    assert "too little" in by_species["Lavender"]["reason"]


def test_recommend_plants_for_light_handles_unknown_catalog_species():
    _repo_module.init_db()
    conn = _repo_module.get_db_connection()
    conn.execute("DELETE FROM plant_catalog;")
    conn.execute("DELETE FROM user_profiles;")
    conn.commit()
    conn.close()

    _repo_module.add_plant(user_id="local_user", species="Unknownia", placement="outdoor")

    from app.agent import recommend_plants_for_light

    result = recommend_plants_for_light(light_tier=2)
    entry = result["catalog_fit"][0]
    assert entry["species"] == "Unknownia"
    assert entry["fits"] is None
    assert "no light data" in entry["reason"].lower()
