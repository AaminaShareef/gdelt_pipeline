"""
Stage 2 — GDELT BigQuery Fetch
Executes generated queries against GDELT's Global Knowledge Graph (GKG)
via Google BigQuery, or the GDELT DOC 2.0 API for material/commodity queries.

Routing (matches query_generator.ENTITY_TYPE_ROUTING):
  - supplier, facility, logistics  → BigQuery GKG (geo + theme filtering)
  - material                       → GDELT DOC 2.0 API (keyword search)

Output per article record:
{
  "url":         str,   # DocumentIdentifier — primary key for Stage 4 fetch
  "source":      str,   # SourceCommonName
  "date":        str,   # ISO datetime string
  "tone":        float, # Negative = bad news
  "themes":      list,  # V2Themes codes e.g. ["LABOR_STRIKE", "ENV_DISASTER"]
  "orgs":        list,  # Organisations mentioned
  "locations":   list,  # Location names mentioned
  "entity_type": str,   # From the query object that found this article
  "entity_name": str,   # Which entity this article was retrieved for
  "query_used":  str    # The keyword phrase used
}

Environment variables required:
  GOOGLE_APPLICATION_CREDENTIALS  path to BigQuery service account JSON
                                   (only needed for BigQuery route)
  GDELT_DAYS                       lookback window in days (default: 7)
  GDELT_MAX_ARTICLES_PER_QUERY     cap per entity query (default: 500)
"""

import os
import json
import re
import requests
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LOOKBACK_DAYS = int(os.environ.get("GDELT_DAYS", "7"))
MAX_PER_QUERY = int(os.environ.get("GDELT_MAX_ARTICLES_PER_QUERY", "500"))
DOC_API_MAX = int(os.environ.get("GDELT_DOC_API_MAX", "250"))

# GDELT DOC 2.0 API endpoint
GDELT_DOC_API_URL = "https://api.gdeltproject.org/api/v2/doc/doc"

# ---------------------------------------------------------------------------
# Trusted source whitelist (applied in BigQuery WHERE clause)
# Tier A: wire services, Tier B: major nationals, Tier C: trade press
# ---------------------------------------------------------------------------

TRUSTED_SOURCES = {
    # Tier A — Wire services
    "reuters", "associated press", "bloomberg", "agence france presse",
    "ap", "afp", "dow jones",
    # Tier B — Major nationals / international
    "financial times", "bbc news", "bbc", "nikkei", "wall street journal",
    "wsj", "south china morning post", "scmp", "the guardian", "the economist",
    "new york times", "washington post", "le monde", "der spiegel",
    "al jazeera", "nhk world", "yonhap", "xinhua",  # kept for monitoring, flag separately
    # Tier C — Trade / domain press
    "lloyd's list", "lloyds list", "supply chain dive", "freightwaves",
    "mining weekly", "mining.com", "metal bulletin", "platts",
    "journal of commerce", "american shipper", "drewry", "tradewindsnews",
    "seatrade maritime", "splash247", "hellenicshippingnews",
    "semiconductors today", "eetimes", "techcrunch",  # for electronics supply chain
}

# Build a SQL-safe tuple string for BigQuery IN clause
_TRUSTED_SOURCES_SQL = "(" + ", ".join(f"'{s}'" for s in sorted(TRUSTED_SOURCES)) + ")"


# ---------------------------------------------------------------------------
# BigQuery route helpers
# ---------------------------------------------------------------------------

def _bigquery_available() -> bool:
    """Return True if google-cloud-bigquery is installed and credentials exist."""
    try:
        from google.cloud import bigquery  # noqa: F401
        return bool(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"))
    except ImportError:
        return False


def _build_bigquery_sql(keywords: str, days: int = LOOKBACK_DAYS, limit: int = MAX_PER_QUERY) -> str:
    """
    Build a BigQuery SQL query against gdelt-bq.gdeltv2.gkg_partitioned.

    Filters:
      - DATE partition: last N days
      - SourceCommonName: trusted whitelist
      - DocumentIdentifier LIKE pattern built from keywords
      - V2Themes or Organizations text LIKE patterns from keywords

    Returns full SQL string.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y%m%d")

    # Build keyword LIKE clauses — split on spaces/commas, use first 4 meaningful tokens
    tokens = [t.strip() for t in re.split(r"[\s,]+", keywords) if len(t.strip()) > 2][:4]
    org_conditions = " OR ".join(
        f"LOWER(V2Organizations) LIKE LOWER('%{t}%')" for t in tokens
    )
    theme_conditions = " OR ".join(
        f"LOWER(V2Themes) LIKE LOWER('%{t}%')" for t in tokens
    )
    loc_conditions = " OR ".join(
        f"LOWER(V2Locations) LIKE LOWER('%{t}%')" for t in tokens
    )

    sql = f"""
SELECT
  DocumentIdentifier                          AS url,
  SourceCommonName                            AS source,
  CAST(DATE AS STRING)                        AS date,
  CAST(JSON_EXTRACT_SCALAR(V2Tone, '$[0]') AS FLOAT64) AS tone,
  V2Themes                                    AS themes_raw,
  V2Organizations                             AS orgs_raw,
  V2Locations                                 AS locations_raw
FROM `gdelt-bq.gdeltv2.gkg_partitioned`
WHERE
  _PARTITIONTIME >= TIMESTAMP('{since}')
  AND LOWER(SourceCommonName) IN {_TRUSTED_SOURCES_SQL}
  AND (
    {org_conditions}
    OR {theme_conditions}
    OR {loc_conditions}
  )
  AND DocumentIdentifier IS NOT NULL
  AND DocumentIdentifier != ''
ORDER BY CAST(DATE AS STRING) DESC
LIMIT {limit}
"""
    return sql.strip()


def _run_bigquery(sql: str) -> list:
    """Execute a BigQuery SQL query and return rows as list of dicts."""
    from google.cloud import bigquery

    client = bigquery.Client()
    query_job = client.query(sql)
    rows = list(query_job.result())

    results = []
    for row in rows:
        themes = _parse_gdelt_delimited(row.get("themes_raw", "") or "")
        orgs = _parse_gdelt_delimited(row.get("orgs_raw", "") or "")
        locs = _parse_gdelt_locations(row.get("locations_raw", "") or "")
        results.append({
            "url": row["url"],
            "source": row["source"],
            "date": str(row["date"]),
            "tone": float(row["tone"]) if row["tone"] is not None else 0.0,
            "themes": themes,
            "orgs": orgs,
            "locations": locs,
        })
    return results


# ---------------------------------------------------------------------------
# GDELT DOC 2.0 API route (materials / commodity keywords)
# ---------------------------------------------------------------------------

def _run_doc_api(query: str, days: int = LOOKBACK_DAYS, limit: int = DOC_API_MAX) -> list:
    """
    Call the GDELT DOC 2.0 API with a natural-language query.
    Returns articles with URL, source, date — tone/themes not available here,
    set to defaults so downstream stages handle them gracefully.
    """
    params = {
        "query": query,
        "mode": "artlist",
        "maxrecords": min(limit, 250),  # DOC API hard cap
        "format": "json",
        "timespan": f"{days}d",
        "sort": "DateDesc",
    }

    try:
        resp = requests.get(GDELT_DOC_API_URL, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, json.JSONDecodeError) as e:
        return [{"error": str(e)}]

    articles = data.get("articles", [])
    results = []
    for art in articles:
        source = art.get("domain", "") or art.get("sourcecountry", "")
        # Apply trust filter post-fetch (DOC API has no WHERE clause)
        source_lower = source.lower()
        if not any(ts in source_lower for ts in TRUSTED_SOURCES):
            continue

        results.append({
            "url": art.get("url", ""),
            "source": source,
            "date": art.get("seendate", ""),
            "tone": 0.0,   # DOC API doesn't return tone
            "themes": [],  # Not available from DOC API
            "orgs": [],
            "locations": [],
        })
    return results


# ---------------------------------------------------------------------------
# Parsing helpers for GDELT's semicolon/comma-delimited fields
# ---------------------------------------------------------------------------

def _parse_gdelt_delimited(raw: str, separator: str = ";") -> list:
    """Split a GDELT pipe/semicolon field into a clean list of strings."""
    if not raw:
        return []
    parts = raw.split(separator)
    # GDELT themes are plain strings; orgs may have trailing commas/metadata
    cleaned = []
    for p in parts:
        val = p.strip().split(",")[0].strip()  # drop sub-fields after comma
        if val:
            cleaned.append(val)
    return list(dict.fromkeys(cleaned))  # deduplicate, preserve order


def _parse_gdelt_locations(raw: str) -> list:
    """
    GDELT V2Locations is semicolon-separated blocks.
    Each block: Type#Name#CountryCode#ADM1Code#ADM2Code#Lat#Long#FeatureID
    We extract just the Name (index 1).
    """
    if not raw:
        return []
    names = []
    for block in raw.split(";"):
        parts = block.split("#")
        if len(parts) >= 2 and parts[1].strip():
            names.append(parts[1].strip())
    return list(dict.fromkeys(names))


# ---------------------------------------------------------------------------
# Fallback: scrape GDELT GKG CSV via HTTP when BigQuery creds are absent
# ---------------------------------------------------------------------------

def _run_gdelt_csv_fallback(keywords: str, days: int = LOOKBACK_DAYS) -> list:
    """
    Lightweight fallback when no BigQuery credentials are available.
    Uses the GDELT 2.0 Master CSV list to find recent GKG files, then
    filters them locally.

    NOTE: This is slower and less precise than BigQuery — it downloads
    raw GKG CSV files and filters in-process. Suitable for dev/testing.
    Returns a capped list to avoid flooding context.
    """
    import csv
    import io
    import zipfile

    # GDELT publishes a master file list; we grab the last N*96 entries (15-min intervals)
    MASTER_URL = "http://data.gdeltproject.org/gdeltv2/lastupdate.txt"
    try:
        r = requests.get(MASTER_URL, timeout=15)
        r.raise_for_status()
    except requests.RequestException:
        return []

    # Parse the three lines: size hash url (for events, mentions, gkg)
    gkg_url = None
    for line in r.text.strip().splitlines():
        parts = line.split()
        if len(parts) == 3 and "gkg" in parts[2].lower() and parts[2].endswith(".zip"):
            gkg_url = parts[2]
            break

    if not gkg_url:
        return []

    try:
        zr = requests.get(gkg_url, timeout=30)
        zr.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(zr.content)) as zf:
            csv_name = zf.namelist()[0]
            with zf.open(csv_name) as f:
                reader = csv.reader(io.TextIOWrapper(f, encoding="utf-8", errors="replace"), delimiter="\t")
                tokens = [t.lower() for t in re.split(r"[\s,]+", keywords) if len(t.strip()) > 2][:4]
                results = []
                for row in reader:
                    if len(row) < 10:
                        continue
                    # GKG columns: DATE(0), SourceCollectionIdentifier(1), SourceCommonName(2),
                    # DocumentIdentifier(3), V2Counts(4), V2Themes(5), V2Locations(6),
                    # V2Persons(7), V2Organizations(8), V2Tone(9) ...
                    source = row[2].lower()
                    if not any(ts in source for ts in TRUSTED_SOURCES):
                        continue
                    combined = " ".join(row[5:9]).lower()
                    if not any(tok in combined for tok in tokens):
                        continue
                    try:
                        tone_val = float(row[9].split(",")[0]) if row[9] else 0.0
                    except (ValueError, IndexError):
                        tone_val = 0.0

                    results.append({
                        "url": row[3],
                        "source": row[2],
                        "date": row[0],
                        "tone": tone_val,
                        "themes": _parse_gdelt_delimited(row[5] if len(row) > 5 else ""),
                        "orgs": _parse_gdelt_delimited(row[8] if len(row) > 8 else ""),
                        "locations": _parse_gdelt_locations(row[6] if len(row) > 6 else ""),
                    })
                    if len(results) >= 50:  # cap fallback results
                        break
                return results
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def _fetch_for_query(query_obj: dict) -> list:
    """
    Fetch GDELT articles for a single query object (from query_generator).
    Returns list of article dicts with entity metadata attached.
    """
    entity_type = query_obj.get("entity_type", "unknown")
    entity_name = query_obj.get("entity_name", "")
    search_route = query_obj.get("search_route", "doc_api")
    bq_keywords = query_obj.get("bigquery_keywords", "")
    doc_query = query_obj.get("doc_api_query", "")

    articles = []

    if search_route == "bigquery":
        if _bigquery_available():
            sql = _build_bigquery_sql(bq_keywords)
            try:
                articles = _run_bigquery(sql)
            except Exception as e:
                # Fallback to CSV if BigQuery fails
                articles = _run_gdelt_csv_fallback(bq_keywords)
                articles = [dict(a, _bigquery_error=str(e)) for a in articles]
        else:
            # No BigQuery creds — use CSV fallback for dev
            articles = _run_gdelt_csv_fallback(bq_keywords)
    else:
        # DOC API route (materials)
        articles = _run_doc_api(doc_query)

    # Attach entity provenance to every article
    for art in articles:
        art["entity_type"] = entity_type
        art["entity_name"] = entity_name
        art["query_used"] = bq_keywords if search_route == "bigquery" else doc_query

    return articles


def fetch_articles_for_queries(
    query_objects: list,
    max_workers: int = 4,
    deduplicate_urls: bool = True,
) -> list:
    """
    Run all query objects (output of query_generator.generate_queries_for_profile)
    through GDELT fetch in parallel.

    Args:
        query_objects:    list of query dicts from Stage 1
        max_workers:      parallel threads (keep low to respect BigQuery quotas)
        deduplicate_urls: if True, an article URL that appears for multiple
                          entities is merged — entity associations combined,
                          duplicate URL rows removed

    Returns:
        Flat list of article dicts ready for Stage 3 metadata pre-filter.
    """
    all_articles = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_fetch_for_query, q): q for q in query_objects
        }
        for future in as_completed(futures):
            try:
                articles = future.result()
                all_articles.extend(articles)
            except Exception as e:
                q = futures[future]
                all_articles.append({
                    "url": "",
                    "source": "",
                    "date": "",
                    "tone": 0.0,
                    "themes": [],
                    "orgs": [],
                    "locations": [],
                    "entity_type": q.get("entity_type", ""),
                    "entity_name": q.get("entity_name", ""),
                    "query_used": q.get("bigquery_keywords", ""),
                    "fetch_error": str(e),
                })

    if deduplicate_urls:
        all_articles = _merge_by_url(all_articles)

    return all_articles


def _merge_by_url(articles: list) -> list:
    """
    When the same URL appears for multiple entity queries, merge the rows:
    - entity_name and entity_type become lists
    - Other fields (source, date, tone, themes, orgs, locations) taken from first occurrence
    - Blank-URL rows (fetch errors) are preserved as-is
    """
    seen: dict[str, dict] = {}
    no_url = []

    for art in articles:
        url = art.get("url", "").strip()
        if not url:
            no_url.append(art)
            continue

        if url not in seen:
            merged = dict(art)
            merged["entity_name"] = [art["entity_name"]] if art.get("entity_name") else []
            merged["entity_type"] = [art["entity_type"]] if art.get("entity_type") else []
            seen[url] = merged
        else:
            existing = seen[url]
            name = art.get("entity_name", "")
            etype = art.get("entity_type", "")
            if name and name not in existing["entity_name"]:
                existing["entity_name"].append(name)
            if etype and etype not in existing["entity_type"]:
                existing["entity_type"].append(etype)
            # Merge theme/org/location lists
            for field in ("themes", "orgs", "locations"):
                combined = list(dict.fromkeys(existing.get(field, []) + art.get(field, [])))
                existing[field] = combined

    return list(seen.values()) + no_url


# ---------------------------------------------------------------------------
# Save / load helpers (mirrors query_generator's flat-file philosophy)
# ---------------------------------------------------------------------------

def save_fetch_results(articles: list, client_id: int, run_dir: str = "data/runs") -> str:
    """
    Persist article records to disk as JSON for the current run.
    Path: data/runs/client_<id>/stage2_articles.json
    Returns the path written.
    """
    client_dir = os.path.join(run_dir, f"client_{client_id}")
    os.makedirs(client_dir, exist_ok=True)
    path = os.path.join(client_dir, "stage2_articles.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(articles, f, indent=2, ensure_ascii=False)
    return path


def load_fetch_results(client_id: int, run_dir: str = "data/runs") -> list:
    """Load persisted Stage 2 results for a client. Returns [] if not found."""
    path = os.path.join(run_dir, f"client_{client_id}", "stage2_articles.json")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    # Minimal smoke test: run one DOC API query and one CSV fallback
    test_queries = [
        {
            "entity_type": "material",
            "entity_name": "Lithium",
            "location": "Atacama Desert, Chile",
            "bigquery_keywords": "lithium Atacama Chile mining strike drought shortage",
            "doc_api_query": "lithium Atacama Chile mining disruption shortage",
            "search_route": "doc_api",
        },
        {
            "entity_type": "logistics",
            "entity_name": "Port of Busan",
            "location": "Busan, South Korea",
            "bigquery_keywords": "Port Busan congestion closure typhoon strike delay",
            "doc_api_query": "Port Busan congestion strike shipping delay",
            "search_route": "bigquery",  # will use CSV fallback if no BQ creds
        },
    ]

    print("Running Stage 2 smoke test...\n")
    results = fetch_articles_for_queries(test_queries, max_workers=2)
    print(f"Total articles fetched: {len(results)}")
    for art in results[:3]:
        print(json.dumps(art, indent=2))

    if "--save" in sys.argv:
        path = save_fetch_results(results, client_id=0)
        print(f"\nResults saved to: {path}")