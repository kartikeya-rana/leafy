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
import sqlite3
import os
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Union
from pydantic import BaseModel, Field

# Note on Multi-User Authentication:
# For now, we assume a single default user_id = "local_user" because the playground
# does not have authentication support. Multi-user authentication is planned as future work.

class PlacementEnum(str, Enum):
    INDOOR = "indoor"
    OUTDOOR = "outdoor"

class UserProfile(BaseModel):
    user_id: str
    location_text: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    resolved_name: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class PlantCatalogItem(BaseModel):
    id: Optional[int] = None
    user_id: str
    species: str
    nickname: Optional[str] = None
    photo_path: Optional[str] = None
    placement: PlacementEnum
    last_watered_date: Optional[datetime] = None
    added_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

# Default SQLite database path
DB_PATH = os.environ.get("LEAFY_DB_PATH", os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "leafy.db")))

def get_db_connection() -> sqlite3.Connection:
    """Creates and returns a connection to the SQLite database.
    
    NOTE ON CONCURRENCY:
    In a multi-threaded production deployment, concurrent state updates on SQLite could 
    result in database locking issues (sqlite3.OperationalError: database is locked).
    To handle concurrent updates in production, we would need:
    1. Row-level locking (e.g., SELECT ... FOR UPDATE if using a RDBMS like PostgreSQL), OR
    2. Optimistic versioning / optimistic concurrency control (adding a version column 
       to rows and checking it during updates), OR
    3. Proper write-ahead logging (WAL) mode and busy timeout handlers in SQLite.
    """
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # Enable foreign keys
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn

def init_db() -> None:
    """Initializes database tables if they do not exist."""
    conn = get_db_connection()
    try:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS user_profiles (
            user_id TEXT PRIMARY KEY,
            location_text TEXT,
            lat REAL,
            lon REAL,
            resolved_name TEXT,
            created_at TEXT NOT NULL
        );
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS plant_catalog (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            species TEXT NOT NULL,
            nickname TEXT,
            photo_path TEXT,
            placement TEXT NOT NULL,
            last_watered_date TEXT,
            added_at TEXT NOT NULL,
            weather_tolerance_json TEXT,
            FOREIGN KEY (user_id) REFERENCES user_profiles(user_id)
        );
        """)
        # Backfill for DBs created before weather_tolerance_json existed.
        existing_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(plant_catalog);").fetchall()
        }
        if "weather_tolerance_json" not in existing_columns:
            conn.execute("ALTER TABLE plant_catalog ADD COLUMN weather_tolerance_json TEXT;")
        conn.commit()
    finally:
        conn.close()

def parse_dt(val: Optional[str]) -> Optional[datetime]:
    """Helper to parse ISO datetime strings back to datetime objects."""
    if not val:
        return None
    try:
        return datetime.fromisoformat(val)
    except ValueError:
        return None

def parse_weather_tolerance(val: Optional[str]) -> Optional[dict]:
    """Helper to parse the stored weather_tolerance JSON string back to a dict."""
    if not val:
        return None
    try:
        return json.loads(val)
    except ValueError:
        return None

def get_or_create_profile(user_id: str) -> UserProfile:
    """Gets an existing user profile or creates a default one if it doesn't exist."""
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT user_id, location_text, lat, lon, resolved_name, created_at FROM user_profiles WHERE user_id = ?",
            (user_id,)
        ).fetchone()
        
        if row:
            return UserProfile(
                user_id=row["user_id"],
                location_text=row["location_text"],
                lat=row["lat"],
                lon=row["lon"],
                resolved_name=row["resolved_name"],
                created_at=datetime.fromisoformat(row["created_at"])
            )
        else:
            created_at = datetime.now(timezone.utc)
            conn.execute(
                "INSERT INTO user_profiles (user_id, created_at) VALUES (?, ?)",
                (user_id, created_at.isoformat())
            )
            conn.commit()
            return UserProfile(
                user_id=user_id,
                created_at=created_at
            )
    finally:
        conn.close()

def update_location(user_id: str, location_text: str, lat: float, lon: float, resolved_name: str) -> UserProfile:
    """Updates the location parameters for a user profile."""
    conn = get_db_connection()
    try:
        # Ensure profile exists first
        get_or_create_profile(user_id)
        conn.execute(
            """UPDATE user_profiles 
               SET location_text = ?, lat = ?, lon = ?, resolved_name = ? 
               WHERE user_id = ?""",
            (location_text, lat, lon, resolved_name, user_id)
        )
        conn.commit()
        
        row = conn.execute(
            "SELECT user_id, location_text, lat, lon, resolved_name, created_at FROM user_profiles WHERE user_id = ?",
            (user_id,)
        ).fetchone()
        
        return UserProfile(
            user_id=row["user_id"],
            location_text=row["location_text"],
            lat=row["lat"],
            lon=row["lon"],
            resolved_name=row["resolved_name"],
            created_at=datetime.fromisoformat(row["created_at"])
        )
    finally:
        conn.close()

def add_plant(
    user_id: str,
    species: str,
    nickname: Optional[str] = None,
    photo_path: Optional[str] = None,
    placement: Union[str, PlacementEnum] = "indoor",
    last_watered_date: Optional[datetime] = None,
    weather_tolerance: Optional[dict] = None,
) -> PlantCatalogItem:
    """Adds a new plant to the user's catalog."""
    if isinstance(placement, str):
        placement = PlacementEnum(placement.lower())

    conn = get_db_connection()
    try:
        # Ensure user profile exists
        get_or_create_profile(user_id)

        added_at = datetime.now(timezone.utc)
        last_watered_str = last_watered_date.isoformat() if last_watered_date else None
        # We no longer store weather tolerance on the catalog item
        weather_tolerance_str = None

        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO plant_catalog
               (user_id, species, nickname, photo_path, placement, last_watered_date, added_at, weather_tolerance_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, species, nickname, photo_path, placement.value, last_watered_str, added_at.isoformat(), weather_tolerance_str)
        )
        plant_id = cursor.lastrowid
        conn.commit()

        return PlantCatalogItem(
            id=plant_id,
            user_id=user_id,
            species=species,
            nickname=nickname,
            photo_path=photo_path,
            placement=placement,
            last_watered_date=last_watered_date,
            added_at=added_at,
        )
    finally:
        conn.close()

def list_plants(user_id: str) -> list[PlantCatalogItem]:
    """Retrieves all plants belonging to a user, ordered by addition date."""
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM plant_catalog WHERE user_id = ? ORDER BY added_at DESC",
            (user_id,)
        ).fetchall()
        
        return [
            PlantCatalogItem(
                id=row["id"],
                user_id=row["user_id"],
                species=row["species"],
                nickname=row["nickname"],
                photo_path=row["photo_path"],
                placement=PlacementEnum(row["placement"]),
                last_watered_date=parse_dt(row["last_watered_date"]),
                added_at=datetime.fromisoformat(row["added_at"]),
            )
            for row in rows
        ]
    finally:
        conn.close()

def get_plant(plant_id: int) -> Optional[PlantCatalogItem]:
    """Retrieves a specific plant by its ID."""
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT * FROM plant_catalog WHERE id = ?",
            (plant_id,)
        ).fetchone()

        if not row:
            return None

        return PlantCatalogItem(
            id=row["id"],
            user_id=row["user_id"],
            species=row["species"],
            nickname=row["nickname"],
            photo_path=row["photo_path"],
            placement=PlacementEnum(row["placement"]),
            last_watered_date=parse_dt(row["last_watered_date"]),
            added_at=datetime.fromisoformat(row["added_at"]),
        )
    finally:
        conn.close()

def update_plant_state(
    plant_id: int,
    placement: Optional[Union[str, PlacementEnum]] = None,
    last_watered_date: Optional[datetime] = None
) -> Optional[PlantCatalogItem]:
    """Updates the dynamic state (placement and/or last watered timestamp) of a plant."""
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT id FROM plant_catalog WHERE id = ?", (plant_id,)).fetchone()
        if not row:
            return None
            
        updates = []
        params = []
        
        if placement is not None:
            if isinstance(placement, str):
                placement = PlacementEnum(placement.lower())
            updates.append("placement = ?")
            params.append(placement.value)
            
        if last_watered_date is not None:
            updates.append("last_watered_date = ?")
            params.append(last_watered_date.isoformat())
            
        if not updates:
            return get_plant(plant_id)
            
        params.append(plant_id)
        query = f"UPDATE plant_catalog SET {', '.join(updates)} WHERE id = ?"
        conn.execute(query, tuple(params))
        conn.commit()
        
        return get_plant(plant_id)
    finally:
        conn.close()

def delete_plant(plant_id: int) -> bool:
    """Deletes a plant from the catalog if it exists. Returns True if deleted, False otherwise."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM plant_catalog WHERE id = ?", (plant_id,))
        if not cursor.fetchone():
            return False
        cursor.execute("DELETE FROM plant_catalog WHERE id = ?", (plant_id,))
        conn.commit()
        return True
    finally:
        conn.close()

# Auto-initialize database on import
init_db()
