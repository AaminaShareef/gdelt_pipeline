"""
relevance_filter.py
====================
Stage 3 of the pipeline: a cheap, fast LLM pass over GDELT METADATA ONLY
(themes, organizations, locations, tone — no full article text yet) to
drop articles that are obviously irrelevant to the client before paying
the cost of scraping + deep analysis.

Input:  the list of GDELT rows returned by gdelt_fetch.run_pipeline()'s
        BigQuery step (before scraping), OR the post-scrape "results" list
        if you want to re-filter after the fact. Either works because this
        module only reads metadata fields, not "snippet".

Output: the same list of articles, each with two new fields added:
            "prefilter_score"  (float 0-1)
            "prefilter_reason" (str, one line)
        Articles scoring below PREFILTER_THRESHOLD are dropped from the
        returned list entirely (not just flagged), so the caller's next
        stage only ever sees survivors.

Model: meta-llama/llama-3.2-3b-instruct:free via OpenRouter.
       Chosen because this step runs at high volume (every article BigQuery
       returns) and only needs a rough relevance estimate, not deep
       reasoning — see SCDI_System_Documentation.docx Section 5.
"""

from llm_client import call_llm, PREFILTER_MODEL

PREFILTER_THRESHOLD = 0.40

SYSTEM_PROMPT = """You are a supply chain risk triage assistant. You will be given:
1. A short description of a client's supply chain (their suppliers, materials, logistics nodes, and facilities).
2. Metadata extracted by GDELT for ONE news article (themes, organizations, locations, tone) — NOT the full article text.

Your job is to estimate how likely this article is to be relevant to a supply chain disruption affecting this specific client, based on the metadata alone.

Score from 0.0 (definitely irrelevant) to 1.0 (very likely relevant).
Be generous toward borderline cases — this is a cheap first-pass filter, not a final judgment. A deeper model will review survivors later. Only score low (below 0.3) when the metadata clearly has nothing to do with the client's entities or with any disruption-related theme.

Respond with JSON only, in this exact shape:
{"score": 0.0, "reason": "one short sentence explaining the score"}
"""


def _profile_summary(profile: dict) -> str:
    """Compact one-paragraph description of the client's supply chain for the prompt."""
    supplier_names = [s["name"] for s in profile.get("tier1_suppliers", [])]
    materials = [m["commodity"] for m in profile.get("raw_materials", [])]
    ports = profile.get("logistics", {}).get("ports", [])
    carriers = profile.get("logistics", {}).get("carriers", [])
    facilities = [f["location"] for f in profile.get("own_facilities", [])]

    parts = []
    if supplier_names:
        parts.append(f"Suppliers: {', '.join(supplier_names)}")
    if materials:
        parts.append(f"Raw materials: {', '.join(materials)}")
    if ports:
        parts.append(f"Ports: {', '.join(ports)}")
    if carriers:
        parts.append(f"Carriers: {', '.join(carriers)}")
    if facilities:
        parts.append(f"Own facilities: {', '.join(facilities)}")

    return f"Client '{profile.get('client_id', 'unknown')}'. " + "; ".join(parts) + "."


def _article_metadata_block(article: dict) -> str:
    """Builds the metadata-only text block sent to the LLM for one article."""
    return (
        f"Anchor entity matched: {article.get('anchor', 'unknown')} ({article.get('query_type', 'unknown')})\n"
        f"Source domain: {article.get('domain', 'unknown')}\n"
        f"GDELT themes: {article.get('themes', '') or '(none extracted)'}\n"
        f"GDELT locations: {article.get('locations', '') or '(none extracted)'}\n"
        f"GDELT organizations: {article.get('organizations', '') or '(none extracted)'}\n"
        f"Tone score: {article.get('tone', 'unknown')}"
    )


def score_article(profile: dict, article: dict) -> dict:
    """
    Scores a single article's metadata against the client profile.
    Returns {"score": float, "reason": str}. On LLM failure, returns a
    neutral score (0.5) so a transient API error doesn't silently drop
    a possibly-relevant article — the deep scorer downstream will still
    catch true negatives.
    """
    user_prompt = (
        f"CLIENT SUPPLY CHAIN:\n{_profile_summary(profile)}\n\n"
        f"ARTICLE METADATA:\n{_article_metadata_block(article)}\n\n"
        "Score this article's relevance to the client's supply chain disruption risk."
    )

    result = call_llm(
        model=PREFILTER_MODEL,
        system_prompt=SYSTEM_PROMPT,
        user_prompt=user_prompt,
        json_mode=True,
        max_tokens=120,
        temperature=0.1,
    )

    if not result["ok"] or not result.get("parsed"):
        return {"score": 0.5, "reason": "Pre-filter LLM call failed; passed through as neutral."}

    parsed = result["parsed"]
    score = parsed.get("score")
    reason = parsed.get("reason", "")

    try:
        score = float(score)
        score = max(0.0, min(1.0, score))
    except (TypeError, ValueError):
        score = 0.5
        reason = reason or "Could not parse score; defaulted to neutral."

    return {"score": score, "reason": reason}


def filter_articles(profile: dict, articles: list, progress_callback=None) -> dict:
    """
    Runs the pre-filter over a list of articles (dicts as produced by
    gdelt_fetch.py — must contain at least: anchor, query_type, domain,
    themes, locations, organizations, tone).

    Returns:
        {
            "passed": [article, ...]   # score >= PREFILTER_THRESHOLD, with
                                        # prefilter_score / prefilter_reason added
            "dropped": [article, ...]  # below threshold, same fields added
            "n_input": int,
            "n_passed": int,
            "n_dropped": int,
        }
    """
    def _log(msg):
        if progress_callback:
            progress_callback(msg)

    passed, dropped = [], []

    for i, article in enumerate(articles, 1):
        result = score_article(profile, article)
        article = dict(article)  # don't mutate caller's list in place
        article["prefilter_score"] = result["score"]
        article["prefilter_reason"] = result["reason"]

        if result["score"] >= PREFILTER_THRESHOLD:
            passed.append(article)
        else:
            dropped.append(article)

        if i % 25 == 0 or i == len(articles):
            _log(f"Pre-filter: {i}/{len(articles)} scored ({len(passed)} passed so far)")

    return {
        "passed": passed,
        "dropped": dropped,
        "n_input": len(articles),
        "n_passed": len(passed),
        "n_dropped": len(dropped),
    }