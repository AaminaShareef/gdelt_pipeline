"""
query_generator.py
===================
Stage 1 — Query Generation  (see SCDI System Documentation, Section 3).

Converts a client supply chain profile (any client, not hardcoded) into a
list of GDELT GKG / BigQuery search specs. Each spec is a dict:
    {"type": ..., "anchor": ..., "where": <SQL boolean expression>, ...}

Per the design doc, Stage 1 is driven by a lightweight LLM
(Llama 3.3 70B via OpenRouter) that turns every profile entity — each
supplier, raw material, logistics node, and facility — into ONE short,
disruption-focused query combining: the entity name, its geographic
context, and a disruption vocabulary appropriate to that entity type
(doc section 3.1 / 3.2). That is the actual Stage 1 behaviour this module
now implements: generate_queries() calls the LLM first.

If the LLM is unreachable (no API key, network down, malformed response)
this module degrades gracefully to a deterministic, rule-based generator
(generate_queries_rule_based, the original logic) so the pipeline never
hard-fails. Callers can also force the deterministic path with
use_llm=False (handy for discover/dev mode or unit tests).

TRUSTED SOURCES are no longer a single hardcoded global list. Per the
user's requirement, each profile's trusted-source set is the union of:
    GLOBAL        -- wire services & major international press (static)
    COUNTRY-LEVEL -- major national outlets per country present in the
                     profile (static seed dict where we already know one)
    LOCAL         -- smaller in-country/regional outlets. For any country
                     not in the static seed, an LLM is asked to identify
                     a handful of reputable local outlets, since these
                     can't be hardcoded for an arbitrary client profile.
    TRADE PRESS   -- sector trade press (energy, shipping, chemicals...),
                     relevant by domain rather than by country.
See resolve_trusted_domains().

This is still primarily a logic module — no Flask, no BigQuery client.
The only I/O it performs is the optional outbound LLM call, which is
isolated in _call_llm() and is fully injectable for testing.
gdelt_fetch.py imports generate_queries() and runs the SQL it produces.
"""

import json
import os
import re
import urllib.request
import urllib.error

# ==========================================================================
# LLM CONFIG  (Stage 1 model per doc section 3.2: Llama 3.3 70B / OpenRouter)
# ==========================================================================
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
QUERY_GEN_MODEL = os.environ.get("SCDI_QUERY_GEN_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
# Escape hatch for dev/test/offline runs -- forces the deterministic path.
DISABLE_LLM = os.environ.get("SCDI_DISABLE_LLM_QUERYGEN", "").strip().lower() in ("1", "true", "yes")


def _call_llm(system_prompt: str, user_prompt: str, model: str = QUERY_GEN_MODEL,
              temperature: float = 0.2, timeout: int = 30) -> str:
    """
    Minimal OpenRouter chat-completion caller (stdlib only -- no 'requests'
    dependency, since this module aims to stay light). Raises
    RuntimeError on any failure so callers can decide how to fall back;
    it never raises a network-library-specific exception type.
    """
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is not set; cannot call LLM.")

    payload = {
        "model": model,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    req = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return body["choices"][0]["message"]["content"]
    except (urllib.error.URLError, KeyError, IndexError, ValueError, TimeoutError, OSError) as e:
        raise RuntimeError(f"LLM call failed: {e}") from e


def _extract_json(text: str):
    """
    LLMs often wrap JSON in markdown fences or add a sentence of preamble.
    Strip fences, then pull out the first top-level [...] or {...} block
    and parse it. Raises ValueError if nothing parseable is found.
    """
    text = text.strip()
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    match = re.search(r"(\[.*\]|\{.*\})", text, re.DOTALL)
    if not match:
        raise ValueError("No JSON found in LLM response.")
    return json.loads(match.group(1))


# ==========================================================================
# TRUSTED SOURCES
#   Resolved per-profile as a union of three tiers (doc 4.2 / 4.3, and the
#   user's requirement to combine global + country-level + local sources,
#   with local sources identifiable via LLM).
# ==========================================================================

# GLOBAL -- wire services & major international press. Relevant to every
# client regardless of where their supply chain sits.
GLOBAL_DOMAINS = [
    "reuters.com", "apnews.com", "afp.com", "bloomberg.com",
    "bbc.com", "bbc.co.uk", "ft.com", "wsj.com", "theguardian.com",
    "nytimes.com", "washingtonpost.com", "cnbc.com", "aljazeera.com",
]

# COUNTRY-LEVEL -- major national outlets we already know about, keyed by
# lowercased country name. This is a free, instant cache consulted before
# ever calling the LLM for that country. Extend over time; it's a cache,
# not a ceiling -- any country missing here falls through to the LLM.
COUNTRY_LEVEL_DOMAINS = {
    "south korea": ["koreaherald.com", "yna.co.kr"],
    "china": ["scmp.com", "caixinglobal.com"],
    "taiwan": ["taipeitimes.com", "focustaiwan.tw"],
    "japan": ["japantimes.co.jp", "asia.nikkei.com", "nikkei.com"],
    "india": ["thehindu.com", "economictimes.indiatimes.com"],
    "netherlands": ["nltimes.nl", "dutchnews.nl"],
    "chile": ["santiagotimes.cl"],
    "zambia": ["znbc.co.zm", "daily-mail.co.zm"],
    "democratic republic of congo": ["radiookapi.net"],
    "malaysia": ["thestar.com.my", "nst.com.my"],
    "uae": ["gulfnews.com", "thenationalnews.com", "khaleejtimes.com"],
    "saudi arabia": ["arabnews.com"],
    "singapore": ["channelnewsasia.com", "straitstimes.com"],
}

# TRADE PRESS -- sector trade publications. Relevant by domain (energy,
# shipping, chemicals, mining...) rather than by country, so this is not
# part of the per-country resolution below; it's always included.
TRADE_PRESS_DOMAINS = [
    "oilprice.com", "rigzone.com", "offshore-energy.biz",
    "naturalgasworld.com", "lngworldnews.com", "energyintel.com",
    "spglobal.com", "platts.com",
    "supplychaindive.com", "freightwaves.com", "joc.com",
    "tradewindsnews.com", "lloydslist.com", "seatrade-maritime.com",
    "hellenicshippingnews.com", "marinetraffic.com",
    "icis.com", "chemweek.com", "echemi.com", "miningweekly.com",
]

# In-process cache of LLM-identified local sources, keyed by country, so
# a single generate_queries() call never asks the LLM the same country
# twice even if it appears as both a supplier location and a port.
_LOCAL_SOURCE_CACHE = {}


def _country_key(location: str) -> str:
    """
    Best-effort country extraction from a free-text location string, e.g.
    'Busan, South Korea' -> 'south korea'. Profiles are user-submitted
    free text with no structured country field, so this is intentionally
    forgiving: take the text after the last comma, or the whole string if
    there's no comma.
    """
    if not location:
        return ""
    parts = [p.strip() for p in location.split(",")]
    return parts[-1].lower()


def identify_local_sources(country: str, use_llm: bool = True) -> list:
    """
    Returns a list of local/regional news domains for `country`.
      1. Static COUNTRY_LEVEL_DOMAINS seed (free, instant) -- checked by
         the caller before this function is even invoked for "known"
         countries; this function focuses on the LLM/cache path.
      2. In-process cache, to avoid repeat LLM calls within one run.
      3. Otherwise, ask the LLM (Llama 3.3 70B) to suggest reputable
         local outlets for that country.
    Always returns a list (possibly empty) -- never raises, so a failed
    LLM call here can't take down query generation for the whole profile.
    """
    key = country.strip().lower()
    if not key:
        return []
    if key in _LOCAL_SOURCE_CACHE:
        return _LOCAL_SOURCE_CACHE[key]
    if not use_llm or DISABLE_LLM:
        return []

    system_prompt = (
        "You identify reputable local and national news websites for "
        "supply-chain risk monitoring. Respond with JSON only: a flat "
        "array of bare domains (e.g. \"example.com\"), no protocol, no "
        "paths, no commentary, 3-5 domains maximum."
    )
    user_prompt = (
        f"List 3-5 reputable local or national news/business outlets "
        f"based in {country} that report on labour disputes, trade "
        f"policy, logistics, severe weather, and industrial incidents "
        f"relevant to that country."
    )
    try:
        raw = _call_llm(system_prompt, user_prompt)
        domains = _extract_json(raw)
        cleaned = []
        for d in domains:
            if not isinstance(d, str):
                continue
            d = d.strip().lower()
            d = re.sub(r"^https?://", "", d)
            d = re.sub(r"^www\.", "", d)
            d = d.split("/")[0]
            if d:
                cleaned.append(d)
        _LOCAL_SOURCE_CACHE[key] = cleaned
        return cleaned
    except (RuntimeError, ValueError):
        _LOCAL_SOURCE_CACHE[key] = []
        return []


def _profile_locations(profile: dict) -> list:
    """Every free-text location string that appears anywhere in the profile."""
    locs = [s["location"] for s in profile.get("tier1_suppliers", [])]
    locs += [f["location"] for f in profile.get("own_facilities", [])]
    locs += profile.get("logistics", {}).get("ports", [])
    for m in profile.get("raw_materials", []):
        locs += m.get("origin_regions", [])
    return [l for l in locs if l]


def resolve_trusted_domains(profile: dict, use_llm: bool = True) -> dict:
    """
    Builds this profile's trusted-source set as the union of global,
    country-level, local, and trade-press domains (see module docstring).
    Returns a dict with each tier broken out -- useful for the dashboard's
    transparency principle (doc Principle 4: every filter is inspectable)
    -- plus an "all" key with the deduplicated union used for the SQL
    domain filter itself.
    """
    countries = sorted({
        _country_key(loc) for loc in _profile_locations(profile) if _country_key(loc)
    })

    country_level = []
    local = []
    for country in countries:
        if country in COUNTRY_LEVEL_DOMAINS:
            country_level.extend(COUNTRY_LEVEL_DOMAINS[country])
        else:
            local.extend(identify_local_sources(country, use_llm=use_llm))

    all_domains = list(dict.fromkeys(  # de-dup, preserve first-seen order
        GLOBAL_DOMAINS + country_level + local + TRADE_PRESS_DOMAINS
    ))

    return {
        "global": GLOBAL_DOMAINS,
        "country_level": country_level,
        "local": local,
        "trade_press": TRADE_PRESS_DOMAINS,
        "all": all_domains,
    }


def is_trusted(domain: str, trusted_domains) -> bool:
    """
    Post-fetch safety net (doc Principle 5): confirms a given
    SourceCommonName is in the trusted set, for use even in discover_mode
    where the SQL domain filter itself was skipped.
    """
    if not domain:
        return False
    return domain.strip().lower() in {d.lower() for d in trusted_domains}


MIN_VIABLE_TOKENS = 60


# ==========================================================================
# DISRUPTION THEME CODES (GDELT GKG V2Themes)
#   Kept as the deterministic fallback's vocabulary, and also OR'd in
#   alongside the LLM's free-text disruption keywords for the LLM path:
#   the LLM is good at nuance specific to one entity (e.g. "rare earth
#   export ban"), the controlled-vocab codes are good at recall on
#   attributes GDELT tags strictly and consistently (e.g. LABOR_STRIKE).
#   Prefix-based: matching 'NATURAL_DISASTER' via LIKE also catches every
#   sub-type (_FLOOD, _EARTHQUAKE, _TYPHOON, ...) for free.
# ==========================================================================
DOMAIN_THEMES = {
    "conflict": [
        "ARMEDCONFLICT", "WB_2462_POLITICAL_VIOLENCE_AND_WAR", "MILITARY",
        "TERROR", "SANCTIONS", "BLOCKADE", "MARITIME_INCIDENT",
        "MARITIME_PIRACY", "SEIGE", "STATE_OF_EMERGENCY",
    ],
    "energy": [
        "ECON_OILPRICE", "ENV_OIL", "ENV_NATURALGAS", "ECON_GASOLINEPRICE",
        "WB_507_ENERGY_AND_EXTRACTIVES", "WB_2298_REFINERIES",
        "WB_2299_PIPELINES", "FUELPRICES",
    ],
    "logistics": [
        "WB_135_TRANSPORT", "WB_793_TRANSPORT_AND_LOGISTICS_SERVICES",
        "WB_167_PORTS", "WB_1817_CONGESTION", "WB_1805_WATERWAYS",
        "CRISISLEX_C04_LOGISTICS_TRANSPORT", "CLOSURE", "DELAY",
    ],
    "commodity": [
        "SHORTAGE", "ECON_TRADE_DISPUTE", "WB_698_TRADE",
        "EPU_CATS_TRADE_POLICY", "WB_778_NON_TARIFF_MEASURES",
        "ECON_BOYCOTT", "BAN", "WB_1079_COMMODITIES_AND_RESOURCES",
    ],
    "labour": [
        "STRIKE", "PROTEST", "ECON_UNIONS", "WB_1670_TRADE_UNIONS",
        "WB_855_LABOR_MARKETS", "UNEMPLOYMENT",
    ],
    "weather": [
        "NATURAL_DISASTER", "MANMADE_DISASTER", "DISASTER_FIRE",
        "WB_820_DISASTER_RISK_MANAGEMENT", "POWER_OUTAGE", "EVACUATION",
    ],
    "pandemic": [
        "HEALTH_PANDEMIC", "WB_2167_PANDEMICS", "TAX_DISEASE_OUTBREAK",
        "WB_2165_HEALTH_EMERGENCIES", "SOC_QUARANTINE",
    ],
}

ALL_THEME_CODES = [t for codes in DOMAIN_THEMES.values() for t in codes]

# Which DOMAIN_THEMES bucket(s) to OR in for each entity_type, used both
# by the LLM path (as a recall backstop alongside its own keywords) and
# the deterministic fallback path.
ENTITY_TYPE_THEME_BUCKETS = {
    "supplier": ["commodity", "labour", "weather"],
    "raw_material": ["energy", "commodity"],
    "logistics": ["logistics", "conflict"],
    "facility": ["weather", "labour", "conflict"],
}


def _theme_codes_for_entity_type(entity_type: str) -> list:
    buckets = ENTITY_TYPE_THEME_BUCKETS.get(entity_type, list(DOMAIN_THEMES.keys()))
    codes = []
    for b in buckets:
        codes.extend(DOMAIN_THEMES.get(b, []))
    return codes or ALL_THEME_CODES


# ==========================================================================
# SQL HELPERS
# ==========================================================================
def _sql_escape(s: str) -> str:
    """Escape single quotes for safe inlining into a SQL string literal."""
    return s.replace("'", "''")


def _domains_in_clause(domains) -> str:
    """
    Build:  SourceCommonName IN ('reuters.com', 'apnews.com', ...)
    If `domains` is falsy (None/empty -- e.g. discover_mode), returns the
    literal 'TRUE' so the clause is a no-op rather than an invalid IN ().
    """
    if not domains:
        return "TRUE"
    quoted = ", ".join(f"'{_sql_escape(d)}'" for d in domains)
    return f"SourceCommonName IN ({quoted})"


def _theme_or_clause(theme_codes) -> str:
    """
    Build an OR group matching any GDELT GKG theme code:
        (V2Themes LIKE '%CODE1%' OR V2Themes LIKE '%CODE2%' ...)
    """
    ors = [f"V2Themes LIKE '%{_sql_escape(code)}%'" for code in theme_codes]
    return "(" + " OR ".join(ors) + ")"


# ==========================================================================
# PROFILE VALIDATION
#   Accepts a profile dict in the same shape as before, but now this is
#   USER INPUT, not a hardcoded constant -- so we validate defensively.
# ==========================================================================
class ProfileValidationError(Exception):
    pass


REQUIRED_TOP_LEVEL_KEYS = {
    "client_id", "tier1_suppliers", "raw_materials", "logistics", "own_facilities"
}


def validate_profile(profile: dict) -> dict:
    """
    Validates and normalises a user-submitted client profile.
    Raises ProfileValidationError with a human-readable message on failure.
    Returns the profile with missing optional sub-keys filled in as empty
    lists/dicts so downstream code never has to guard against KeyError.
    """
    if not isinstance(profile, dict):
        raise ProfileValidationError("Client profile must be a JSON object.")

    missing = REQUIRED_TOP_LEVEL_KEYS - set(profile.keys())
    if missing:
        raise ProfileValidationError(
            f"Client profile is missing required field(s): {', '.join(sorted(missing))}"
        )

    if not profile["client_id"] or not isinstance(profile["client_id"], str):
        raise ProfileValidationError("client_id must be a non-empty string.")

    # tier1_suppliers: list of {name, provides, location}
    suppliers = profile.get("tier1_suppliers") or []
    if not isinstance(suppliers, list):
        raise ProfileValidationError("tier1_suppliers must be a list.")
    for i, s in enumerate(suppliers):
        if not isinstance(s, dict) or not s.get("name") or not s.get("location"):
            raise ProfileValidationError(
                f"tier1_suppliers[{i}] must include at least 'name' and 'location'."
            )
        s.setdefault("provides", "")

    # raw_materials: list of {commodity, origin_regions: [..]}
    materials = profile.get("raw_materials") or []
    if not isinstance(materials, list):
        raise ProfileValidationError("raw_materials must be a list.")
    for i, m in enumerate(materials):
        if not isinstance(m, dict) or not m.get("commodity"):
            raise ProfileValidationError(
                f"raw_materials[{i}] must include 'commodity'."
            )
        m.setdefault("origin_regions", [])
        if not isinstance(m["origin_regions"], list):
            raise ProfileValidationError(
                f"raw_materials[{i}].origin_regions must be a list."
            )

    # logistics: {ports: [..], carriers: [..]}
    logistics = profile.get("logistics") or {}
    if not isinstance(logistics, dict):
        raise ProfileValidationError("logistics must be an object.")
    logistics.setdefault("ports", [])
    logistics.setdefault("carriers", [])
    if not isinstance(logistics["ports"], list) or not isinstance(logistics["carriers"], list):
        raise ProfileValidationError("logistics.ports and logistics.carriers must be lists.")
    profile["logistics"] = logistics

    # own_facilities: list of {location, type}
    facilities = profile.get("own_facilities") or []
    if not isinstance(facilities, list):
        raise ProfileValidationError("own_facilities must be a list.")
    for i, f in enumerate(facilities):
        if not isinstance(f, dict) or not f.get("location"):
            raise ProfileValidationError(
                f"own_facilities[{i}] must include 'location'."
            )
        f.setdefault("type", "")

    # At least one entity overall, or there is nothing to search for.
    if not (suppliers or materials or logistics["ports"] or logistics["carriers"] or facilities):
        raise ProfileValidationError(
            "Client profile has no suppliers, materials, logistics nodes, or facilities. "
            "At least one entity is required to generate search queries."
        )

    return profile


# ==========================================================================
# STAGE 1, LLM PATH
#   Per doc section 3.2: one lightweight LLM call, one query per entity.
# ==========================================================================
def _entity_list(profile: dict) -> list:
    """
    Flattens the profile into the entity records the doc's Stage 1 prompt
    expects -- one row per supplier, per (material, origin region) pair,
    per port, per carrier, per facility. Matches the doc's example
    ("Supplier: KorTech Semiconductors, Busan, South Korea" etc).
    """
    entities = []
    for s in profile.get("tier1_suppliers", []):
        entities.append({"entity_type": "supplier", "entity": s["name"], "location": s["location"]})
    for m in profile.get("raw_materials", []):
        regions = m.get("origin_regions") or [""]
        for r in regions:
            entities.append({"entity_type": "raw_material", "entity": m["commodity"], "location": r})
    for port in profile.get("logistics", {}).get("ports", []):
        entities.append({"entity_type": "logistics", "entity": port, "location": port})
    for carrier in profile.get("logistics", {}).get("carriers", []):
        entities.append({"entity_type": "logistics", "entity": carrier, "location": ""})
    for f in profile.get("own_facilities", []):
        label = f.get("type") or "Facility"
        entities.append({"entity_type": "facility", "entity": label, "location": f["location"]})
    return entities


def _llm_generate_entity_queries(profile: dict, model: str = QUERY_GEN_MODEL) -> list:
    """
    Core Stage 1 LLM call. Asks the model for one disruption-focused query
    per entity (doc section 3.2's exact prompt shape), plus a short list
    of disruption keywords per entity we can use to build the SQL filter.
    Returns a list of dicts:
        {entity_type, entity, location, query, disruption_keywords}
    Raises RuntimeError/ValueError on any failure -- the caller decides
    whether to fall back to the deterministic generator.
    """
    entities = _entity_list(profile)
    if not entities:
        return []

    system_prompt = (
        "You are a supply chain analyst. Convert the client profile into "
        "GDELT search queries. For each entity, produce ONE query string "
        "under 10 words combining: the entity name, its geographic "
        "context, and a short disruption vocabulary relevant to that "
        "specific entity type (e.g. strike, shutdown, sanctions, "
        "shortage, flood, congestion -- pick words that fit the entity, "
        "not a generic list). Also return 4-6 individual disruption "
        "keywords separately for the same entity. Output valid JSON "
        "only: an array of objects with keys entity_type, entity, "
        "location, query, disruption_keywords. No prose, no markdown."
    )
    entity_lines = "\n".join(
        f"- {e['entity_type']}: {e['entity']}" + (f", {e['location']}" if e["location"] else "")
        for e in entities
    )
    user_prompt = (
        f"Client: {profile.get('client_id')}. Entities:\n{entity_lines}\n"
        "Generate disruption-focused search queries for each entity."
    )

    raw = _call_llm(system_prompt, user_prompt, model=model)
    parsed = _extract_json(raw)
    if not isinstance(parsed, list):
        raise ValueError("Expected a JSON array of entity queries.")

    cleaned = []
    for item in parsed:
        if not isinstance(item, dict) or not item.get("entity"):
            continue
        item.setdefault("entity_type", "entity")
        item.setdefault("location", "")
        item.setdefault("query", item["entity"])
        kws = item.get("disruption_keywords") or []
        item["disruption_keywords"] = [str(k).strip().lower() for k in kws if str(k).strip()]
        cleaned.append(item)
    return cleaned


def _build_where_for_entity(entity_record: dict, domains) -> str:
    """
    Turns one LLM-generated entity record into a SQL boolean expression:
        (entity name OR location appears in Organizations/DocumentIdentifier/Locations)
        AND (LLM disruption keywords OR matching controlled-vocab theme codes)
        AND domain filter
    """
    name = _sql_escape(entity_record["entity"].lower())
    location = _sql_escape((entity_record.get("location") or "").lower())

    name_clause_parts = [
        f"LOWER(V2Organizations) LIKE '%{name}%'",
        f"LOWER(DocumentIdentifier) LIKE '%{name}%'",
    ]
    if location:
        name_clause_parts.append(f"LOWER(V2Locations) LIKE '%{location}%'")
    name_clause = "(" + " OR ".join(name_clause_parts) + ")"

    keyword_parts = []
    for kw in entity_record.get("disruption_keywords", []):
        kw_esc = _sql_escape(kw)
        keyword_parts.append(f"LOWER(DocumentIdentifier) LIKE '%{kw_esc}%'")
        keyword_parts.append(f"UPPER(V2Themes) LIKE '%{kw_esc.upper()}%'")
    theme_codes = _theme_codes_for_entity_type(entity_record["entity_type"])
    keyword_parts.append(_theme_or_clause(theme_codes))
    keyword_clause = "(" + " OR ".join(keyword_parts) + ")"

    domain_clause = _domains_in_clause(domains)

    return f"{name_clause} AND {keyword_clause} AND {domain_clause}"


# ==========================================================================
# STAGE 1, DETERMINISTIC FALLBACK PATH
#   Original prong-based logic, kept verbatim in behaviour, but now
#   parameterised by a resolved `domains` list instead of a single
#   hardcoded constant -- used when the LLM is disabled or unreachable.
# ==========================================================================
def generate_queries_rule_based(profile: dict, domains=None) -> list:
    """
    Returns a list of dicts: {type, anchor, where}. Deterministic,
    template-based query construction -- no LLM call. This is the
    fallback path generate_queries() uses if the LLM path fails.
    `domains` should be the "all" list from resolve_trusted_domains(),
    or None/[] to skip the domain filter (discover/dev mode).
    """
    queries = []

    def _and_dom():
        clause = _domains_in_clause(domains)
        return f"AND {clause}" if clause != "TRUE" else ""

    # --- PRONG 1: entity (Tier-1 suppliers) --------------------------------
    for s in profile.get("tier1_suppliers", []):
        name = _sql_escape(s["name"].lower())
        where = (
            f"(LOWER(V2Organizations) LIKE '%{name}%' "
            f"OR LOWER(DocumentIdentifier) LIKE '%{name}%') "
            f"{_and_dom()}"
        )
        queries.append({"type": "entity", "anchor": s["name"], "where": where})

    # --- PRONG 2: geographic (supplier + facility + port locations) -------
    locations = [s["location"] for s in profile.get("tier1_suppliers", [])]
    locations += [f["location"] for f in profile.get("own_facilities", [])]
    locations += profile.get("logistics", {}).get("ports", [])
    for loc in locations:
        loc_esc = _sql_escape(loc.lower())
        theme_clause = _theme_or_clause(ALL_THEME_CODES)
        where = (
            f"LOWER(V2Locations) LIKE '%{loc_esc}%' "
            f"AND {theme_clause} "
            f"{_and_dom()}"
        )
        queries.append({"type": "geo", "anchor": loc, "where": where})

    # --- PRONG 3: commodity (raw materials + their origin regions) --------
    commodity_themes = DOMAIN_THEMES["energy"] + DOMAIN_THEMES["commodity"]
    for m in profile.get("raw_materials", []):
        commodity = _sql_escape(m["commodity"].lower())
        theme_clause = _theme_or_clause(commodity_themes)
        where = (
            f"(LOWER(DocumentIdentifier) LIKE '%{commodity}%' "
            f"OR LOWER(V2Organizations) LIKE '%{commodity}%') "
            f"AND {theme_clause} "
            f"{_and_dom()}"
        )
        queries.append({"type": "commodity", "anchor": m["commodity"], "where": where})

        for region in m.get("origin_regions", []):
            region_esc = _sql_escape(region.lower())
            where_r = (
                f"LOWER(V2Locations) LIKE '%{region_esc}%' "
                f"AND (LOWER(DocumentIdentifier) LIKE '%{commodity}%' "
                f"     OR LOWER(V2Organizations) LIKE '%{commodity}%') "
                f"AND {theme_clause} "
                f"{_and_dom()}"
            )
            queries.append({
                "type": "commodity_origin",
                "anchor": f"{region}/{m['commodity']}",
                "where": where_r,
            })

    # --- PRONG 4: carrier (logistics carriers) -----------------------------
    carrier_themes = DOMAIN_THEMES["logistics"] + DOMAIN_THEMES["conflict"]
    for carrier in profile.get("logistics", {}).get("carriers", []):
        carrier_esc = _sql_escape(carrier.lower())
        theme_clause = _theme_or_clause(carrier_themes)
        where = (
            f"(LOWER(V2Organizations) LIKE '%{carrier_esc}%' "
            f"OR LOWER(DocumentIdentifier) LIKE '%{carrier_esc}%') "
            f"AND {theme_clause} "
            f"{_and_dom()}"
        )
        queries.append({"type": "carrier", "anchor": carrier, "where": where})

    return queries


# ==========================================================================
# STAGE 1, PUBLIC ENTRY POINT
# ==========================================================================
def generate_queries(profile: dict, domain_filter: bool = True, use_llm: bool = True) -> list:
    """
    Stage 1 entry point (doc section 3). Tries the LLM-driven generator
    first -- one query per entity, produced by Llama 3.3 70B per the
    doc's exact prompt shape. Falls back to the deterministic rule-based
    generator if the LLM is disabled, unreachable, or returns something
    unusable, so the pipeline never hard-fails.

    domain_filter=True  (default, production): restricts every query to
        this profile's resolved trusted-source set (global + country-
        level + local + trade press -- see resolve_trusted_domains()).
    domain_filter=False (discover/dev only): no domain constraint in
        SQL; BigQuery returns all matching articles regardless of
        outlet. Use is_trusted() downstream as a post-fetch safety net
        if you still want to flag/sort by trust in this mode.
    use_llm=True (default): attempt the LLM path before falling back.
        Pass False to force the deterministic path (e.g. in tests, or
        when SCDI_DISABLE_LLM_QUERYGEN should be the only kill switch).
    """
    trust = resolve_trusted_domains(profile, use_llm=use_llm)
    domains = trust["all"] if domain_filter else None

    if use_llm and not DISABLE_LLM:
        try:
            entity_records = _llm_generate_entity_queries(profile)
            if entity_records:
                queries = []
                for rec in entity_records:
                    where = _build_where_for_entity(rec, domains)
                    anchor = rec["entity"]
                    if rec.get("location"):
                        anchor = f"{rec['entity']} ({rec['location']})"
                    queries.append({
                        "type": rec["entity_type"],
                        "anchor": anchor,
                        "where": where,
                        "llm_query": rec.get("query"),
                    })
                return queries
        except (RuntimeError, ValueError):
            # LLM path failed for any reason -- degrade to deterministic
            # generation rather than breaking the whole pipeline run.
            pass

    return generate_queries_rule_based(profile, domains=domains)


def audit_coverage(profile: dict) -> dict:
    """Returns theme-code counts per disruption domain (for diagnostics/UI)."""
    return {d: len(codes) for d, codes in DOMAIN_THEMES.items()}