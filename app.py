"""
SCDI Pipeline — Flask app entry point.

Serves the client profile intake UI (templates/index.html, static/css,
static/js) and exposes a JSON API for client profile CRUD (backed by
db.py / SQLite) plus Stage 1 query generation (query_generator.py).

Run:
    python app.py

Environment:
    OPENROUTER_API_KEY   required by query_generator.py for Stage 1 calls
    SCDI_DB_PATH         optional, defaults to scdi.db (see db.py)
    FLASK_DEBUG          optional, "1" to enable debug/reload
"""

import os
from flask import Flask, jsonify, request, render_template

import db
import query_generator
import gdelt_fetch

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# Client profile API
# ---------------------------------------------------------------------------

@app.route("/api/clients", methods=["GET"])
def api_list_clients():
    """List all saved client profiles: [{id, client_name, updated_at}, ...]"""
    return jsonify(db.list_clients())


@app.route("/api/clients/<int:client_id>", methods=["GET"])
def api_get_client(client_id):
    """Get one client's full record: {id, client_name, profile, updated_at}"""
    record = db.get_client(client_id)
    if record is None:
        return jsonify({"error": "Client not found"}), 404
    return jsonify(record)


@app.route("/api/clients", methods=["POST"])
def api_create_client():
    """Create a new client profile from posted JSON profile."""
    profile = request.get_json(silent=True)
    if not profile:
        return jsonify({"error": "Request body must be a JSON client profile"}), 400

    record = db.upsert_client(profile)
    db.export_profile_json(record["profile"], record["id"])
    return jsonify(record), 201


@app.route("/api/clients/<int:client_id>", methods=["PUT"])
def api_update_client(client_id):
    """Overwrite an existing client profile with posted JSON profile."""
    profile = request.get_json(silent=True)
    if not profile:
        return jsonify({"error": "Request body must be a JSON client profile"}), 400

    if db.get_client_profile(client_id) is None:
        return jsonify({"error": "Client not found"}), 404

    record = db.upsert_client(profile, client_id=client_id)
    db.export_profile_json(record["profile"], record["id"])
    return jsonify(record)


@app.route("/api/clients/<int:client_id>", methods=["DELETE"])
def api_delete_client(client_id):
    """Delete a client profile."""
    deleted = db.delete_client_profile(client_id)
    if not deleted:
        return jsonify({"error": "Client not found"}), 404
    return jsonify({"deleted": True})


# ---------------------------------------------------------------------------
# Stage 1 — Query Generation
# ---------------------------------------------------------------------------

@app.route("/api/clients/<int:client_id>/queries", methods=["POST"])
def api_generate_queries(client_id):
    """
    Run Stage 1 (Llama 3.3 70B via OpenRouter) for a saved client profile,
    returning one GDELT query object per entity (supplier, material,
    logistics node, facility).
    """
    profile = db.get_client_profile(client_id)
    if profile is None:
        return jsonify({"error": "Client not found"}), 404

    if not os.environ.get("OPENROUTER_API_KEY"):
        return jsonify({"error": "OPENROUTER_API_KEY is not set on the server"}), 500

    try:
        queries = query_generator.generate_queries_for_profile(profile)
    except Exception as e:
        return jsonify({"error": f"Query generation failed: {e}"}), 500

    return jsonify({"client_id": client_id, "queries": queries})


# ---------------------------------------------------------------------------
# Stage 2 — GDELT Fetch
# ---------------------------------------------------------------------------

@app.route("/api/clients/<int:client_id>/fetch", methods=["POST"])
def api_fetch_articles(client_id):
    """
    Run Stage 2 (GDELT fetch) for a saved client profile.

    Expects a JSON body with previously generated queries:
      { "queries": [ ...query objects from Stage 1... ] }

    Or pass regenerate=true to re-run Stage 1 first:
      { "regenerate": true }

    Returns { client_id, article_count, articles: [...] }
    and persists results to data/runs/client_<id>/stage2_articles.json.
    """
    profile = db.get_client_profile(client_id)
    if profile is None:
        return jsonify({"error": "Client not found"}), 404

    body = request.get_json(silent=True) or {}
    queries = body.get("queries")

    # If no queries provided, try to regenerate from profile (requires OPENROUTER_API_KEY)
    if not queries:
        if not os.environ.get("OPENROUTER_API_KEY"):
            return jsonify({
                "error": "Provide 'queries' in the request body, or set OPENROUTER_API_KEY to auto-generate them."
            }), 400
        try:
            queries = query_generator.generate_queries_for_profile(profile)
        except Exception as e:
            return jsonify({"error": f"Query generation failed: {e}"}), 500

    try:
        articles = gdelt_fetch.fetch_articles_for_queries(queries)
        save_path = gdelt_fetch.save_fetch_results(articles, client_id)
    except Exception as e:
        return jsonify({"error": f"GDELT fetch failed: {e}"}), 500

    return jsonify({
        "client_id": client_id,
        "article_count": len(articles),
        "saved_to": save_path,
        "articles": articles,
    })


@app.route("/api/clients/<int:client_id>/fetch", methods=["GET"])
def api_get_fetched_articles(client_id):
    """Return previously saved Stage 2 results for a client."""
    if db.get_client_profile(client_id) is None:
        return jsonify({"error": "Client not found"}), 404

    articles = gdelt_fetch.load_fetch_results(client_id)
    return jsonify({
        "client_id": client_id,
        "article_count": len(articles),
        "articles": articles,
    })


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.route("/api/health", methods=["GET"])
def api_health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    db.init_db()
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=5000, debug=debug)