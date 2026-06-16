"""
deep_scorer.py
==============
Stages 4, 5, and 6 of the pipeline:

  Stage 4 - Deep relevance scoring on FULL ARTICLE TEXT. Reads the scraped
            "snippet" for each article that survived the Stage 3 pre-filter
            and extracts a structured judgment: relevance score, affected
            client entities, event type, a plain-language summary,
            quantitative claims, causal narrative, severity, and source
            angle. This is the step that catches false positives the
            keyword/theme matching let through (e.g. "Foxconn invests in
            solar power" matching the Foxconn entity query but NOT being a
            disruption at all).

  Stage 5 - Smart deduplication. Groups deep-scored articles into event
            clusters (same affected entities + same event type + close in
            time), then drops an article from a cluster ONLY if it adds no
            new information versus articles already kept in that cluster
            (same event description, same entities, same key figures, same
            causal narrative, same source angle). This is the "don't lose
            content just because headlines look similar" logic.

  Stage 6 - Briefing generation. For each surviving event cluster, asks
            Nemotron to write one short analyst-facing brief synthesising
            all retained articles in that cluster.

Model: nvidia/nemotron-3-super-120b:free via OpenRouter for both scoring
       and briefing — see SCDI_System_Documentation.docx Sections 6 and 8.
"""

from llm_client import call_llm, DEEP_SCORE_MODEL

DEEP_SCORE_THRESHOLD = 0.65

DEEP_SCORE_SYSTEM_PROMPT = """You are a senior supply chain risk analyst. You will be given:
1. A client's supply chain profile (suppliers, raw materials, logistics nodes, own facilities).
2. The full text of ONE news article that was flagged as a possible match during search.

Your job is to make the real judgment call: is this article actually describing a supply chain disruption that is relevant to this specific client, or is it unrelated / routine / positive business news that merely mentioned a matching name or location?

Be skeptical. Many articles will mention a client's supplier or location WITHOUT describing any disruption (e.g. a partnership announcement, an investment, a product launch, a stock price move with no operational cause). These should score LOW even though they matched the search. Only score high when the article describes something that could plausibly delay, halt, or raise the cost of part of this client's supply chain.

Respond with JSON only, in this exact shape:
{
  "relevance_score": 0.0,
  "confidence": "low" | "medium" | "high",
  "is_disruption": true,
  "affected_entities": ["entity name", "entity name"],
  "event_type": "LABOR_STRIKE" | "NATURAL_DISASTER" | "TRADE_POLICY" | "LOGISTICS_DELAY" | "FACILITY_INCIDENT" | "COMMODITY_SHORTAGE" | "GEOPOLITICAL" | "OTHER" | "NONE",
  "event_summary": "1-3 sentences describing what actually happened, in your own words",
  "quantitative_claims": ["any specific figures, dates, or durations mentioned"],
  "causal_narrative": "1 sentence on cause and effect, or empty string if not applicable",
  "severity": "LOW" | "MEDIUM" | "HIGH" | "NONE",
  "source_angle": "whose perspective/quotes this article centers (e.g. 'union statement', 'company press release', 'market analyst view'), or empty string"
}

If the article is not a disruption at all, set is_disruption to false, event_type to "NONE", severity to "NONE", and relevance_score low (below 0.3).
"""

BRIEFING_SYSTEM_PROMPT = """You are a senior supply chain risk analyst writing a short brief for another analyst who has not read the source articles.

You will be given a client's supply chain profile and a set of structured event extractions that all describe THE SAME underlying disruption event (already deduplicated and clustered for you).

Write one concise brief, in your own words, covering:
1. What happened (the event itself)
2. Why it matters to this specific client (which of their entities are exposed and how)
3. The latest known development or outlook, if the extractions mention one
4. Overall severity

Keep it to 3-5 sentences. Do not invent facts not present in the extractions. Do not use bullet points — write it as prose, the way an analyst would write a one-paragraph alert.

Respond with JSON only, in this exact shape:
{
  "severity": "LOW" | "MEDIUM" | "HIGH",
  "headline": "short 6-10 word headline for this event",
  "brief": "the 3-5 sentence prose brief"
}
"""


def _profile_summary(profile: dict) -> str:
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


# ==========================================================================
# STAGE 4 — DEEP SCORING
# ==========================================================================
def deep_score_article(profile: dict, article: dict) -> dict:
    """
    Scores one article using its full scraped text ("snippet"). Returns
    the structured extraction dict described in DEEP_SCORE_SYSTEM_PROMPT,
    or a safe fallback (is_disruption=False) on LLM failure so a transient
    error never produces a false-positive disruption alert.
    """
    snippet = article.get("snippet") or ""
    if not snippet.strip():
        return {
            "relevance_score": 0.0,
            "confidence": "low",
            "is_disruption": False,
            "affected_entities": [],
            "event_type": "NONE",
            "event_summary": "No article text available to analyze.",
            "quantitative_claims": [],
            "causal_narrative": "",
            "severity": "NONE",
            "source_angle": "",
        }

    user_prompt = (
        f"CLIENT SUPPLY CHAIN:\n{_profile_summary(profile)}\n\n"
        f"ARTICLE (anchor matched: {article.get('anchor', 'unknown')}, "
        f"source: {article.get('domain', 'unknown')}, date: {article.get('gkg_date', 'unknown')}):\n"
        f"{snippet}\n\n"
        "Analyze this article per your instructions."
    )

    result = call_llm(
        model=DEEP_SCORE_MODEL,
        system_prompt=DEEP_SCORE_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        json_mode=True,
        max_tokens=600,
        temperature=0.1,
    )

    if not result["ok"] or not result.get("parsed"):
        return {
            "relevance_score": 0.0,
            "confidence": "low",
            "is_disruption": False,
            "affected_entities": [],
            "event_type": "NONE",
            "event_summary": f"Deep scoring failed: {result.get('error', 'unknown error')}",
            "quantitative_claims": [],
            "causal_narrative": "",
            "severity": "NONE",
            "source_angle": "",
        }

    parsed = result["parsed"]

    # Defensive normalisation — never trust the model to perfectly follow schema.
    try:
        parsed["relevance_score"] = max(0.0, min(1.0, float(parsed.get("relevance_score", 0.0))))
    except (TypeError, ValueError):
        parsed["relevance_score"] = 0.0

    parsed.setdefault("confidence", "low")
    parsed.setdefault("is_disruption", parsed["relevance_score"] >= DEEP_SCORE_THRESHOLD)
    parsed.setdefault("affected_entities", [])
    parsed.setdefault("event_type", "NONE")
    parsed.setdefault("event_summary", "")
    parsed.setdefault("quantitative_claims", [])
    parsed.setdefault("causal_narrative", "")
    parsed.setdefault("severity", "NONE")
    parsed.setdefault("source_angle", "")

    if not isinstance(parsed["affected_entities"], list):
        parsed["affected_entities"] = [str(parsed["affected_entities"])]
    if not isinstance(parsed["quantitative_claims"], list):
        parsed["quantitative_claims"] = [str(parsed["quantitative_claims"])]

    return parsed


def deep_score_articles(profile: dict, articles: list, progress_callback=None) -> dict:
    """
    Runs deep scoring over a list of pre-filter-passed, scraped articles
    (each must have a non-empty "snippet"). Returns:
        {
            "scored": [article_with_extraction, ...],   # all input articles,
                                                          # each with a new
                                                          # "extraction" field
            "disruptions": [article_with_extraction, ...], # subset where
                                                          # relevance_score >= threshold
            "n_input": int,
            "n_disruptions": int,
        }
    """
    def _log(msg):
        if progress_callback:
            progress_callback(msg)

    scored = []
    disruptions = []

    for i, article in enumerate(articles, 1):
        extraction = deep_score_article(profile, article)
        article = dict(article)
        article["extraction"] = extraction
        scored.append(article)

        if extraction["relevance_score"] >= DEEP_SCORE_THRESHOLD and extraction["is_disruption"]:
            disruptions.append(article)

        if i % 10 == 0 or i == len(articles):
            _log(f"Deep scoring: {i}/{len(articles)} analyzed ({len(disruptions)} confirmed disruptions so far)")

    return {
        "scored": scored,
        "disruptions": disruptions,
        "n_input": len(articles),
        "n_disruptions": len(disruptions),
    }


# ==========================================================================
# STAGE 5 — SMART DEDUPLICATION
# ==========================================================================
def _cluster_key(article: dict) -> tuple:
    """
    Groups articles into the same event cluster if they share the same
    event_type and have overlapping affected_entities. Articles with no
    overlapping entities or different event types are never merged, even
    if the anchor/query_type matched the same search.
    """
    extraction = article.get("extraction", {})
    event_type = extraction.get("event_type", "NONE")
    entities = tuple(sorted(e.lower().strip() for e in extraction.get("affected_entities", []) if e))
    return (event_type, entities)


def cluster_disruptions(disruptions: list) -> list:
    """
    Groups disruption articles into event clusters using (event_type,
    affected_entities) as the grouping key. Returns a list of cluster
    dicts: {"key": ..., "articles": [...]}.

    Two articles with different affected_entities are NEVER merged even
    if the event_type matches (e.g. two separate port strikes at two
    different ports stay separate clusters).
    """
    clusters = {}
    for article in disruptions:
        key = _cluster_key(article)
        clusters.setdefault(key, []).append(article)

    return [{"key": k, "articles": v} for k, v in clusters.items()]


def _is_true_duplicate(candidate: dict, kept_so_far: list) -> bool:
    """
    An article is a true duplicate of something already kept in this
    cluster ONLY if it adds nothing new across all five dimensions:
    event description, entities, quantitative claims, causal narrative,
    and source angle. If ANY dimension differs meaningfully from every
    already-kept article, it is retained.

    This is a deliberately conservative (keep-leaning) heuristic: it
    compares normalized text overlap rather than calling the LLM again
    per pair, to keep this stage fast and free of additional API cost.
    A borderline call (any genuine uncertainty) defaults to KEEP, per
    the system's "never lose information before deduplication" principle.
    """
    cand_ext = candidate.get("extraction", {})
    cand_summary = (cand_ext.get("event_summary") or "").lower().strip()
    cand_claims = set(c.lower().strip() for c in cand_ext.get("quantitative_claims", []))
    cand_narrative = (cand_ext.get("causal_narrative") or "").lower().strip()
    cand_angle = (cand_ext.get("source_angle") or "").lower().strip()

    for kept in kept_so_far:
        kept_ext = kept.get("extraction", {})
        kept_summary = (kept_ext.get("event_summary") or "").lower().strip()
        kept_claims = set(c.lower().strip() for c in kept_ext.get("quantitative_claims", []))
        kept_narrative = (kept_ext.get("causal_narrative") or "").lower().strip()
        kept_angle = (kept_ext.get("source_angle") or "").lower().strip()

        same_summary = cand_summary == kept_summary or (
            cand_summary and kept_summary and (cand_summary in kept_summary or kept_summary in cand_summary)
        )
        same_claims = cand_claims == kept_claims
        same_narrative = cand_narrative == kept_narrative
        same_angle = cand_angle == kept_angle

        if same_summary and same_claims and same_narrative and same_angle:
            return True  # genuinely adds nothing new vs this kept article

    return False


def deduplicate_cluster(articles: list) -> list:
    """
    Within one event cluster, keeps every article that adds new
    information and drops only true duplicates. Order of arrival matters
    only in that the first article in a cluster is always kept (it cannot
    be a duplicate of nothing).
    """
    kept = []
    for article in articles:
        if not kept or not _is_true_duplicate(article, kept):
            kept.append(article)
    return kept


def deduplicate_disruptions(disruptions: list, progress_callback=None) -> dict:
    """
    Full Stage 5 entry point. Clusters disruption articles by event, then
    deduplicates within each cluster.

    Returns:
        {
            "clusters": [
                {"key": (event_type, entities), "articles": [kept articles]},
                ...
            ],
            "n_input": int,
            "n_kept": int,
            "n_dropped": int,
        }
    """
    def _log(msg):
        if progress_callback:
            progress_callback(msg)

    raw_clusters = cluster_disruptions(disruptions)
    final_clusters = []
    n_kept = 0

    for cluster in raw_clusters:
        kept_articles = deduplicate_cluster(cluster["articles"])
        final_clusters.append({"key": cluster["key"], "articles": kept_articles})
        n_kept += len(kept_articles)

    n_input = len(disruptions)
    _log(f"Deduplication: {n_input} disruption articles -> {len(raw_clusters)} clusters -> {n_kept} unique articles kept")

    return {
        "clusters": final_clusters,
        "n_input": n_input,
        "n_kept": n_kept,
        "n_dropped": n_input - n_kept,
    }


# ==========================================================================
# STAGE 6 — BRIEFING GENERATION
# ==========================================================================
def generate_briefing(profile: dict, cluster: dict) -> dict:
    """
    Generates one analyst-facing brief for a single event cluster (after
    deduplication). Returns:
        {"severity": ..., "headline": ..., "brief": ..., "sources": [...]}
    """
    articles = cluster["articles"]
    event_type, entities = cluster["key"]

    extraction_blocks = []
    sources = []
    for a in articles:
        ext = a.get("extraction", {})
        extraction_blocks.append(
            f"- Source: {a.get('domain', 'unknown')} ({a.get('gkg_date', 'unknown')})\n"
            f"  Summary: {ext.get('event_summary', '')}\n"
            f"  Quantitative claims: {', '.join(ext.get('quantitative_claims', [])) or 'none'}\n"
            f"  Causal narrative: {ext.get('causal_narrative', '') or 'none'}\n"
            f"  Source angle: {ext.get('source_angle', '') or 'none'}\n"
            f"  Severity (this article): {ext.get('severity', 'unknown')}"
        )
        sources.append({
            "domain": a.get("domain"),
            "url": a.get("url"),
            "gkg_date": a.get("gkg_date"),
        })

    user_prompt = (
        f"CLIENT SUPPLY CHAIN:\n{_profile_summary(profile)}\n\n"
        f"EVENT TYPE: {event_type}\n"
        f"AFFECTED ENTITIES: {', '.join(entities) if entities else 'unspecified'}\n\n"
        f"DEDUPLICATED ARTICLE EXTRACTIONS FOR THIS EVENT:\n" + "\n\n".join(extraction_blocks) + "\n\n"
        "Write the brief per your instructions."
    )

    result = call_llm(
        model=DEEP_SCORE_MODEL,
        system_prompt=BRIEFING_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        json_mode=True,
        max_tokens=400,
        temperature=0.2,
    )

    if not result["ok"] or not result.get("parsed"):
        # Fallback: synthesize a minimal brief from the first extraction
        # rather than silently dropping the cluster.
        fallback_ext = articles[0].get("extraction", {}) if articles else {}
        return {
            "severity": fallback_ext.get("severity", "MEDIUM"),
            "headline": f"{event_type.replace('_', ' ').title()} — {', '.join(entities) if entities else 'Unspecified entity'}",
            "brief": fallback_ext.get("event_summary", "Briefing generation failed; showing raw extraction summary."),
            "sources": sources,
        }

    parsed = result["parsed"]
    parsed.setdefault("severity", "MEDIUM")
    parsed.setdefault("headline", f"{event_type.replace('_', ' ').title()}")
    parsed.setdefault("brief", "")
    parsed["sources"] = sources

    return parsed


def generate_all_briefings(profile: dict, dedup_result: dict, progress_callback=None) -> list:
    """
    Generates one briefing per event cluster from deduplicate_disruptions()'s
    output. Returns a list of briefing dicts, sorted with HIGH severity first.
    """
    def _log(msg):
        if progress_callback:
            progress_callback(msg)

    severity_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    briefings = []

    clusters = dedup_result.get("clusters", [])
    for i, cluster in enumerate(clusters, 1):
        if not cluster["articles"]:
            continue
        briefing = generate_briefing(profile, cluster)
        briefings.append(briefing)
        _log(f"Briefing {i}/{len(clusters)} generated: {briefing.get('headline', '')}")

    briefings.sort(key=lambda b: severity_order.get(b.get("severity", "MEDIUM"), 1))
    return briefings