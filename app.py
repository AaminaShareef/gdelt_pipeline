"""
app.py
======
Flask entry point. Serves the client profile form, accepts a user-submitted
profile (instead of a hardcoded one), runs the GDELT BigQuery + scrape
pipeline against it, persists results, and returns them to the frontend.

Auth note: BigQuery access uses Application Default Credentials from the
gcloud CLI (`gcloud auth application-default login`). No service-account
key file is read here.
"""

import os
import traceback

from flask import Flask, jsonify, render_template, request
from dotenv import load_dotenv

import db
from query_generator import validate_profile, ProfileValidationError
from gdelt_fetch import run_pipeline

load_dotenv()

app = Flask(__name__)

# Developer-only switch. Never exposed in the UI / API request body.
# Set DISCOVER_MODE=true in .env temporarily when onboarding a brand-new
# client whose relevant news domains aren't in TRUSTED_DOMAINS yet.
DISCOVER_MODE = os.environ.get("DISCOVER_MODE", "false").lower() == "true"

db.init_db()


# ==========================================================================
# PAGES
# ==========================================================================
@app.route("/")
def index():
    return render_template("index.html")


# ==========================================================================
# CLIENT PROFILE API
# ==========================================================================
@app.route("/api/profiles", methods=["GET"])
def api_list_profiles():
    return jsonify(db.list_profiles())


@app.route("/api/profiles/<client_id>", methods=["GET"])
def api_get_profile(client_id):
    profile = db.get_profile(client_id)
    if profile is None:
        return jsonify({"error": "Client profile not found."}), 404
    return jsonify(profile)


@app.route("/api/profiles", methods=["POST"])
def api_create_profile():
    """
    Accepts a client profile submitted by the user (via form or JSON body)
    in place of the old hardcoded CLIENT_PROFILE. Validates it, then saves
    it. Does NOT run the pipeline — that's a separate explicit step so a
    user can review/edit a profile before spending BigQuery + scrape time.
    """
    payload = request.get_json(silent=True)
    if payload is None:
        return jsonify({"error": "Request body must be valid JSON."}), 400

    try:
        profile = validate_profile(payload)
    except ProfileValidationError as e:
        return jsonify({"error": str(e)}), 400

    db.save_profile(profile)
    return jsonify({"status": "saved", "client_id": profile["client_id"]}), 201


@app.route("/api/profiles/<client_id>", methods=["DELETE"])
def api_delete_profile(client_id):
    db.delete_profile(client_id)
    return jsonify({"status": "deleted", "client_id": client_id})


# ==========================================================================
# PIPELINE EXECUTION API
# ==========================================================================
@app.route("/api/run/<client_id>", methods=["POST"])
def api_run_pipeline(client_id):
    """
    Runs the GDELT BigQuery fetch + scrape pipeline for an already-saved
    client profile. This replaces the trial script's
    `run_trial(CLIENT_PROFILE, ...)` call at the bottom of the old script —
    the profile now comes from the database (i.e. from whatever the user
    submitted), not from a hardcoded constant.
    """
    profile = db.get_profile(client_id)
    if profile is None:
        return jsonify({"error": f"No saved profile for client_id '{client_id}'."}), 404

    body = request.get_json(silent=True) or {}
    days_back = int(body.get("days_back", 7))
    articles_per_query = int(body.get("articles_per_query", 15))
    max_queries = body.get("max_queries")  # None = no cap
    max_tokens = int(body.get("max_tokens", 500))
    polite_delay = float(body.get("polite_delay", 2.5))

    log_lines = []

    def progress(msg):
        log_lines.append(msg)

    try:
        run_result = run_pipeline(
            profile,
            days_back=days_back,
            articles_per_query=articles_per_query,
            max_queries=max_queries,
            max_tokens=max_tokens,
            polite_delay=polite_delay,
            discover_mode=DISCOVER_MODE,   # dev-only env flag, never user input
            progress_callback=progress,
        )
    except RuntimeError as e:
        # e.g. GOOGLE_CLOUD_PROJECT not set
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "Pipeline run failed.", "detail": str(e)}), 500

    run_id = db.save_run(client_id, run_result)
    run_result["run_id"] = run_id
    run_result["log"] = log_lines

    return jsonify(run_result)


@app.route("/api/runs/<client_id>", methods=["GET"])
def api_list_runs(client_id):
    return jsonify(db.list_runs(client_id))


@app.route("/api/runs/<int:run_id>/detail", methods=["GET"])
def api_get_run(run_id):
    run = db.get_run(run_id)
    if run is None:
        return jsonify({"error": "Run not found."}), 404
    return jsonify(run)


@app.route("/api/runs/<client_id>/latest", methods=["GET"])
def api_latest_run(client_id):
    run = db.latest_run_for_client(client_id)
    if run is None:
        return jsonify({"error": "No runs found for this client yet."}), 404
    return jsonify(run)


if __name__ == "__main__":
    app.run(debug=True, port=5000)