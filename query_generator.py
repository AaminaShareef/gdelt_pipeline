"""
Stage 1 — Query Generation
Converts a client supply chain profile into one targeted GDELT search query
per entity (supplier, raw material, logistics node, facility), using
Llama 3.3 70B via OpenRouter (per system docs: Stage 1 LLM = Llama 3.3 70B,
free tier — structured, templated task, no deep reasoning required).

Each generated query object includes:
  - bigquery_keywords: a short keyword phrase suited to GDELT GKG theme/location filtering
  - doc_api_query:     a natural-language phrase suited to the GDELT DOC 2.0 API

Input profile shape matches the client intake form / db.py exactly:
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

import os
import json
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
QUERY_GEN_MODEL = "meta-llama/llama-3.3-70b-instruct:free"  # Stage 1 model per system docs

SYSTEM_PROMPT = """You are a supply chain intelligence query generator.

Given one supply chain entity (a supplier, raw material, logistics node, or
facility) and its location, generate a focused news search query that would
surface disruption-relevant articles about it.

A good query combines:
1. The entity name (company, commodity, port, facility, carrier)
2. Its geographic context (city/region/country)
3. Likely disruption keywords (e.g. strike, shortage, fire, flood, sanctions,
   export ban, congestion, delay, shutdown, earthquake, drought, labor unrest)

Respond ONLY with a JSON object, no preamble, no markdown fences, in this
exact shape:

{
  "bigquery_keywords": "short keyword phrase, 5-8 words, entity + geo + 2-3 disruption terms",
  "doc_api_query": "natural language search phrase, 6-10 words, entity + geo + disruption context"
}
"""

USER_PROMPT_TEMPLATE = """Entity Type: {entity_type}
Entity Name: {entity_name}
Location / Geo Context: {location}
Additional Context: {context}

Generate the search query JSON for this entity."""


def _call_query_llm(entity_type: str, entity_name: str, location: str, context: str = "") -> dict:
    """Call Llama 3.3 70B via OpenRouter to generate a query for a single entity."""
    payload = {
        "model": QUERY_GEN_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": USER_PROMPT_TEMPLATE.format(
                    entity_type=entity_type,
                    entity_name=entity_name,
                    location=location or "Unspecified",
                    context=context or "N/A",
                ),
            },
        ],
        "temperature": 0.3,
        "max_tokens": 300,
    }

    response = requests.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    content = data["choices"][0]["message"]["content"].strip()

    # Strip accidental markdown fences
    if content.startswith("```"):
        content = content.strip("`")
        if content.startswith("json"):
            content = content[4:].strip()

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        # Fallback: build a naive query from the entity fields
        parsed = {
            "bigquery_keywords": f"{entity_name} {location} disruption shortage delay".strip(),
            "doc_api_query": f"{entity_name} {location} disruption".strip(),
        }

    return {
        "entity_type": entity_type,
        "entity_name": entity_name,
        "location": location,
        "bigquery_keywords": parsed.get("bigquery_keywords", ""),
        "doc_api_query": parsed.get("doc_api_query", ""),
    }


# Entity types that map to specific search routing in the GDELT fetcher
# (used downstream by gdelt_fetch.py to decide BigQuery vs DOC API)
ENTITY_TYPE_ROUTING = {
    "supplier": "bigquery",       # geo-heavy, theme filtering works well
    "facility": "bigquery",       # geo-heavy
    "logistics": "bigquery",      # geo + theme (ports, carriers)
    "material": "doc_api",        # broad commodity keyword search
}


def generate_queries_for_profile(client_profile: dict, max_workers: int = 5) -> list:
    """
    Generate GDELT queries for every entity in a client profile.

    Returns a flat list of query objects:
    {
      "entity_type": "supplier" | "material" | "logistics" | "facility",
      "entity_name": str,
      "location": str,
      "bigquery_keywords": str,
      "doc_api_query": str,
      "search_route": "bigquery" | "doc_api"
    }
    """
    tasks = []

    for s in client_profile.get("suppliers", []):
        tasks.append(("supplier", s["name"], s.get("location", ""), s.get("supplies", "")))

    for m in client_profile.get("materials", []):
        tasks.append(("material", m["name"], m.get("sourced_from", ""), ""))

    for node in client_profile.get("logistics_nodes", []):
        tasks.append(("logistics", node["name"], node.get("location", ""), node.get("role", "")))

    for f in client_profile.get("facilities", []):
        tasks.append(("facility", f["name"], f.get("location", ""), ""))

    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_call_query_llm, entity_type, entity_name, location, context): (
                entity_type, entity_name, location, context
            )
            for entity_type, entity_name, location, context in tasks
        }

        for future in as_completed(futures):
            entity_type, entity_name, location, context = futures[future]
            try:
                query_obj = future.result()
            except Exception as e:
                # Fallback on API failure
                query_obj = {
                    "entity_type": entity_type,
                    "entity_name": entity_name,
                    "location": location,
                    "bigquery_keywords": f"{entity_name} {location} disruption shortage delay".strip(),
                    "doc_api_query": f"{entity_name} {location} disruption".strip(),
                    "error": str(e),
                }

            query_obj["search_route"] = ENTITY_TYPE_ROUTING.get(entity_type, "doc_api")
            results.append(query_obj)

    return results


if __name__ == "__main__":
    # Example: load a profile from SQLite and generate queries for it
    from db import get_client_profile

    profile = get_client_profile(client_id=1)
    if profile is None:
        print("No client profile found. Save one via db.save_client_profile() first.")
    else:
        queries = generate_queries_for_profile(profile)
        print(json.dumps(queries, indent=2))