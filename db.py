"""
SQLite storage for client supply chain profiles.

A client profile is stored as a single JSON blob per client. If a client's
profile is edited, it is overwritten in place (no versioning/history) —
mirrors the frontend's localStorage "scdi_profiles" behaviour.

Expected profile JSON shape (matches the intake form's output exactly):
{
  "client_name": str,
  "suppliers": [
    {"name": str, "supplies": str, "location": str}, ...
  ],
  "materials": [
    {"name": str, "sourced_from": str}, ...
  ],
  "logistics_nodes": [
    {"name": str, "type": str, "role": str, "location": str}, ...
  ],
  "facilities": [
    {"name": str, "location": str}, ...
  ]
}
"""

import sqlite3
import json
import os

DB_PATH = os.environ.get("SCDI_DB_PATH", "scdi.db")
PROFILES_DIR = os.environ.get("SCDI_PROFILES_DIR", os.path.join("data", "client_profiles"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_name TEXT NOT NULL,
    profile_json TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
"""


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    conn.execute(SCHEMA)
    conn.commit()
    conn.close()


def save_client_profile(profile: dict, client_id: int = None) -> int:
    """
    Save a client profile as JSON.

    - If client_id is given and an existing row matches, profile_json is
      fully overwritten (last-write-wins, no history).
    - Otherwise, a new row is inserted.

    Returns the client_id of the saved profile.
    """
    init_db()
    conn = get_connection()
    profile_json = json.dumps(profile)
    client_name = profile.get("client_name", "Unnamed Client")

    if client_id is not None:
        cur = conn.execute("SELECT id FROM clients WHERE id = ?", (client_id,))
        if cur.fetchone() is not None:
            conn.execute(
                "UPDATE clients SET client_name = ?, profile_json = ?, updated_at = datetime('now') WHERE id = ?",
                (client_name, profile_json, client_id),
            )
            conn.commit()
            conn.close()
            return client_id

    cur = conn.execute(
        "INSERT INTO clients (client_name, profile_json) VALUES (?, ?)",
        (client_name, profile_json),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def get_client_profile(client_id: int) -> dict:
    """Return the profile JSON for a given client_id, or None if not found."""
    init_db()
    conn = get_connection()
    cur = conn.execute("SELECT profile_json FROM clients WHERE id = ?", (client_id,))
    row = cur.fetchone()
    conn.close()
    if row is None:
        return None
    return json.loads(row["profile_json"])


def list_clients() -> list:
    """
    Return [{id, client_name, updated_at}, ...] for all stored clients,
    most recently updated first. Matches the shape script.js expects for
    rendering the "Use existing profile" list.
    """
    init_db()
    conn = get_connection()
    cur = conn.execute("SELECT id, client_name, updated_at FROM clients ORDER BY updated_at DESC")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def delete_client_profile(client_id: int) -> bool:
    """Delete a client profile. Returns True if a row was deleted."""
    init_db()
    conn = get_connection()
    cur = conn.execute("DELETE FROM clients WHERE id = ?", (client_id,))
    conn.commit()
    deleted = cur.rowcount > 0
    conn.close()
    return deleted


def export_profile_json(profile: dict, client_id: int) -> str:
    """
    Write the client profile to disk as a standalone JSON file for
    training/data purposes, under PROFILES_DIR (default: data/client_profiles/).

    Filename pattern: client_<client_id>.json — stable across edits, so
    renaming a client overwrites the same file rather than creating a new one.

    Returns the path the file was written to.
    """
    os.makedirs(PROFILES_DIR, exist_ok=True)

    filename = f"client_{client_id}.json"
    filepath = os.path.join(PROFILES_DIR, filename)

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2, ensure_ascii=False)

    return filepath


# ---------------------------------------------------------------------------
# Flask route helpers
#
# These wrap the functions above with dict-friendly returns so they can be
# dropped directly into Flask view functions, e.g.:
#
#   @app.route("/api/clients", methods=["GET"])
#   def api_list_clients():
#       return jsonify(list_clients())
#
#   @app.route("/api/clients/<int:client_id>", methods=["GET"])
#   def api_get_client(client_id):
#       record = get_client(client_id)
#       if record is None:
#           return jsonify({"error": "not found"}), 404
#       return jsonify(record)
#
#   @app.route("/api/clients", methods=["POST"])
#   def api_create_client():
#       profile = request.get_json()
#       return jsonify(upsert_client(profile)), 201
#
#   @app.route("/api/clients/<int:client_id>", methods=["PUT"])
#   def api_update_client(client_id):
#       profile = request.get_json()
#       return jsonify(upsert_client(profile, client_id=client_id))
#
#   @app.route("/api/clients/<int:client_id>", methods=["DELETE"])
#   def api_delete_client(client_id):
#       return jsonify({"deleted": delete_client_profile(client_id)})
# ---------------------------------------------------------------------------


def upsert_client(profile: dict, client_id: int = None) -> dict:
    """
    Create or overwrite a client profile and return the full saved record
    in the shape used by script.js's "scdi_profiles" entries:
    {id, client_name, profile, updated_at}
    """
    saved_id = save_client_profile(profile, client_id=client_id)

    init_db()
    conn = get_connection()
    cur = conn.execute("SELECT id, client_name, updated_at FROM clients WHERE id = ?", (saved_id,))
    row = dict(cur.fetchone())
    conn.close()

    return {
        "id": row["id"],
        "client_name": row["client_name"],
        "profile": profile,
        "updated_at": row["updated_at"],
    }


def get_client(client_id: int) -> dict:
    """
    Return a full client record {id, client_name, profile, updated_at},
    or None if not found.
    """
    init_db()
    conn = get_connection()
    cur = conn.execute("SELECT id, client_name, profile_json, updated_at FROM clients WHERE id = ?", (client_id,))
    row = cur.fetchone()
    conn.close()
    if row is None:
        return None
    return {
        "id": row["id"],
        "client_name": row["client_name"],
        "profile": json.loads(row["profile_json"]),
        "updated_at": row["updated_at"],
    }


if __name__ == "__main__":
    init_db()
    print(f"Initialized database at {DB_PATH}")
    print("Clients:", list_clients())