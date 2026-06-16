"""
db.py
=====
SQLite persistence for client profiles and pipeline run results.
Uses scdi.db (already present in the project root per the existing
file structure).
"""

import json
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.environ.get("SCDI_DB_PATH", "scdi.db")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Creates tables if they do not already exist. Safe to call on every startup."""
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS client_profiles (
            client_id   TEXT PRIMARY KEY,
            profile_json TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS pipeline_runs (
            run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id   TEXT NOT NULL,
            run_json    TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            FOREIGN KEY (client_id) REFERENCES client_profiles(client_id)
        )
    """)

    conn.commit()
    conn.close()


# ==========================================================================
# CLIENT PROFILES
# ==========================================================================
def save_profile(profile: dict):
    """Insert or update a client profile, keyed by client_id."""
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT client_id FROM client_profiles WHERE client_id = ?", (profile["client_id"],))
    exists = cur.fetchone() is not None

    if exists:
        cur.execute(
            "UPDATE client_profiles SET profile_json = ?, updated_at = ? WHERE client_id = ?",
            (json.dumps(profile), now, profile["client_id"]),
        )
    else:
        cur.execute(
            "INSERT INTO client_profiles (client_id, profile_json, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (profile["client_id"], json.dumps(profile), now, now),
        )

    conn.commit()
    conn.close()

    # Also mirror to data/client_profiles/<client_id>.json to match the
    # existing folder convention already in the project.
    os.makedirs(os.path.join("data", "client_profiles"), exist_ok=True)
    file_path = os.path.join("data", "client_profiles", f"{profile['client_id']}.json")
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)


def get_profile(client_id: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT profile_json FROM client_profiles WHERE client_id = ?", (client_id,))
    row = cur.fetchone()
    conn.close()
    return json.loads(row["profile_json"]) if row else None


def list_profiles():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT client_id, created_at, updated_at FROM client_profiles ORDER BY updated_at DESC")
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_profile(client_id: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM client_profiles WHERE client_id = ?", (client_id,))
    conn.commit()
    conn.close()


# ==========================================================================
# PIPELINE RUNS
# ==========================================================================
def save_run(client_id: str, run_result: dict) -> int:
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO pipeline_runs (client_id, run_json, created_at) VALUES (?, ?, ?)",
        (client_id, json.dumps(run_result), now),
    )
    run_id = cur.lastrowid
    conn.commit()
    conn.close()
    return run_id


def get_run(run_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT run_json FROM pipeline_runs WHERE run_id = ?", (run_id,))
    row = cur.fetchone()
    conn.close()
    return json.loads(row["run_json"]) if row else None


def list_runs(client_id: str = None):
    conn = get_connection()
    cur = conn.cursor()
    if client_id:
        cur.execute(
            "SELECT run_id, client_id, created_at FROM pipeline_runs WHERE client_id = ? ORDER BY created_at DESC",
            (client_id,),
        )
    else:
        cur.execute("SELECT run_id, client_id, created_at FROM pipeline_runs ORDER BY created_at DESC")
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def latest_run_for_client(client_id: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT run_json FROM pipeline_runs WHERE client_id = ? ORDER BY created_at DESC LIMIT 1",
        (client_id,),
    )
    row = cur.fetchone()
    conn.close()
    return json.loads(row["run_json"]) if row else None