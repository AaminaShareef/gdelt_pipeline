"""
gdelt_fetch.py
==============
Executes GDELT GKG searches on BigQuery for a given client profile, then
scrapes full article text via trafilatura. This module contains no
hardcoded client data — it operates on whatever profile dict is passed
to run_pipeline().

Auth: uses Application Default Credentials (ADC) via the gcloud CLI.
      Run once on this machine:  gcloud auth application-default login
      No service-account JSON file is used or required.
"""

import os
import re
import time
from datetime import datetime, timedelta, timezone

from google.cloud import bigquery

import trafilatura

from query_generator import (
    generate_queries,
    audit_coverage,
    TRUSTED_DOMAINS_SET,
    MIN_VIABLE_TOKENS,
)

# ==========================================================================
# CONFIG
# ==========================================================================
GCP_PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
GKG_TABLE = "gdelt-bq.gdeltv2.gkg_partitioned"

_WORD_RE = re.compile(r"\S+")


# ==========================================================================
# SQL ASSEMBLY + EXECUTION
# ==========================================================================
def build_sql(where_clause: str, start_dt: datetime, end_dt: datetime, max_records: int) -> str:
    """
    Date filtering uses BOTH:
      - _PARTITIONTIME  (cheap: limits which partitions are scanned)
      - DATE column     (precise: GKG DATE is YYYYMMDDHHMMSS integer)
    English-only: TranslationInfo IS NULL (translated articles carry a tag).
    """
    start_part = start_dt.strftime("%Y-%m-%d")
    end_part = end_dt.strftime("%Y-%m-%d")
    start_num = start_dt.strftime("%Y%m%d%H%M%S")
    end_num = end_dt.strftime("%Y%m%d%H%M%S")

    return f"""
        SELECT
            DocumentIdentifier AS url,
            SourceCommonName   AS domain,
            DATE               AS gkg_date,
            V2Themes           AS themes,
            V2Locations        AS locations,
            V2Organizations    AS organizations,
            V2Tone             AS tone
        FROM `{GKG_TABLE}`
        WHERE _PARTITIONTIME >= TIMESTAMP("{start_part}")
          AND _PARTITIONTIME <= TIMESTAMP("{end_part}")
          AND CAST(DATE AS INT64) >= {start_num}
          AND CAST(DATE AS INT64) <= {end_num}
          AND TranslationInfo IS NULL
          AND DocumentIdentifier IS NOT NULL
          AND ({where_clause})
        ORDER BY DATE DESC
        LIMIT {max_records}
    """


def gdelt_bigquery_search(client, where_clause, start_dt, end_dt, max_records):
    """Run one BigQuery search; return (rows, bytes_processed)."""
    sql = build_sql(where_clause, start_dt, end_dt, max_records)
    job = client.query(sql)
    rows = job.result()
    out = []
    for r in rows:
        out.append({
            "url": r["url"],
            "domain": r["domain"],
            "gkg_date": str(r["gkg_date"]),
            "themes": r["themes"],
            "locations": r["locations"],
            "organizations": r["organizations"],
            "tone": r["tone"],
        })
    return out, job.total_bytes_processed


# ==========================================================================
# DOMAIN CHECK + SCRAPE
# ==========================================================================
def _root_domain(url: str) -> str:
    m = re.search(r"https?://(?:www\.)?([^/]+)", url)
    return m.group(1).lower() if m else ""


def is_trusted(url: str) -> bool:
    domain = _root_domain(url)
    return any(domain == td or domain.endswith("." + td) for td in TRUSTED_DOMAINS_SET)


def truncate_tokens(text: str, max_tokens: int = 500):
    tokens = _WORD_RE.findall(text)
    return " ".join(tokens[:max_tokens]), len(tokens)


def scrape_body(url: str, max_tokens: int = 500, retries: int = 3, base_delay: float = 3.0):
    for attempt in range(retries):
        try:
            downloaded = trafilatura.fetch_url(url)
            if not downloaded:
                return None, 0, 0
            body = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
            if not body:
                return None, 0, 0
            snippet, total = truncate_tokens(body, max_tokens)
            kept = len(_WORD_RE.findall(snippet))
            return snippet, kept, total
        except Exception as e:
            if "429" in str(e) or "Too Many Requests" in str(e):
                wait = base_delay * (2 ** attempt)
                time.sleep(wait)
            else:
                return None, 0, 0
    return None, 0, 0


# ==========================================================================
# MAIN PIPELINE ENTRY POINT
#   This is what app.py calls after a user submits a client profile.
#   `profile` must already be validated by query_generator.validate_profile().
# ==========================================================================
def run_pipeline(
    profile: dict,
    days_back: int = 7,
    articles_per_query: int = 15,
    max_queries: int = None,
    max_tokens: int = 500,
    polite_delay: float = 2.5,
    discover_mode: bool = False,   # default OFF for real users — trusted domains only
    progress_callback=None,        # optional fn(str) for live status (e.g. websocket/log)
):
    """
    Runs the full fetch+scrape pipeline for one client profile and returns
    a result dict ready to be saved to the DB / returned as JSON to the UI.

    discover_mode is a developer-only knob (see app.py) — end users never
    set this themselves. It is False by default so real client runs always
    respect the trusted-domain whitelist.
    """
    if not GCP_PROJECT_ID:
        raise RuntimeError(
            "GOOGLE_CLOUD_PROJECT is not set. Add it to your .env file."
        )

    def _log(msg):
        if progress_callback:
            progress_callback(msg)

    client = bigquery.Client(project=GCP_PROJECT_ID)

    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=days_back)

    queries = generate_queries(profile, domain_filter=not discover_mode)
    if max_queries:
        queries = queries[:max_queries]

    _log(f"Generated {len(queries)} queries for client '{profile['client_id']}'")
    _log(f"Mode: {'DISCOVER (no domain filter)' if discover_mode else 'FILTERED (trusted domains only)'}")

    seen_urls = set()
    results = []
    total_bytes = 0
    n_rows_returned = 0
    n_filtered_domain = 0
    n_filtered_short = 0
    all_domains_seen = {}

    for qi, q in enumerate(queries, 1):
        _log(f"[{qi}/{len(queries)}] {q['type']} :: {q['anchor']}")

        try:
            rows, bytes_scanned = gdelt_bigquery_search(
                client, q["where"], start_dt, end_dt, articles_per_query
            )
        except Exception as e:
            _log(f"  BigQuery error on this query: {str(e)[:200]}")
            continue

        total_bytes += bytes_scanned
        n_rows_returned += len(rows)

        for row in rows:
            url = row["url"]
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            row_domain = row.get("domain") or _root_domain(url)
            all_domains_seen[row_domain] = all_domains_seen.get(row_domain, 0) + 1

            if not discover_mode and not is_trusted(url):
                n_filtered_domain += 1
                continue

            time.sleep(polite_delay)
            snippet, kept, total = scrape_body(url, max_tokens=max_tokens)

            if snippet is not None and total < MIN_VIABLE_TOKENS:
                n_filtered_short += 1
                snippet, kept, total = None, 0, 0

            results.append({
                "anchor": q["anchor"],
                "query_type": q["type"],
                "url": url,
                "domain": row["domain"],
                "gkg_date": row["gkg_date"],
                "tone": row["tone"],
                "scrape_ok": snippet is not None,
                "tokens_total": total,
                "tokens_kept": kept,
                "snippet": snippet,
            })

    ok = sum(1 for r in results if r["scrape_ok"])

    summary = {
        "client_id": profile["client_id"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": GKG_TABLE,
        "date_window": [start_dt.isoformat(), end_dt.isoformat()],
        "discover_mode": discover_mode,
        "n_queries": len(queries),
        "bytes_scanned": total_bytes,
        "n_rows": n_rows_returned,
        "n_filtered_domain": n_filtered_domain,
        "n_filtered_short": n_filtered_short,
        "n_scraped_ok": ok,
        "n_scraped_total": len(results),
        "domains_seen": all_domains_seen,
        "coverage": audit_coverage(profile),
        "results": results,
    }

    _log(
        f"Done. {n_rows_returned} rows returned, {len(results)} attempted, "
        f"{ok} scraped successfully ({total_bytes / 1e9:.3f} GB scanned)."
    )

    return summary