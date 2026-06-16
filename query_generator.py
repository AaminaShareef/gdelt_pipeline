"""
query_generator.py
===================
Converts a client supply chain profile (any client, not hardcoded) into a
list of GDELT GKG / BigQuery search specs. Each spec is a dict:
    {"type": ..., "anchor": ..., "where": <SQL boolean expression>}

This is a pure logic module — no Flask, no BigQuery client, no I/O.
gdelt_fetch.py imports generate_queries() and runs the SQL it produces.
"""

# ==========================================================================
# TRUSTED DOMAINS
#   Two tiers, combined into one set used for both:
#     A) SQL filter   -> SourceCommonName IN (...)   when discover_mode=False
#     B) Post-fetch safety net via is_trusted()       in all modes
# ==========================================================================
TIER_1_DOMAINS = [
    "reuters.com", "apnews.com", "bbc.com", "bbc.co.uk",
    "bloomberg.com", "ft.com", "wsj.com", "theguardian.com",
    "nytimes.com", "washingtonpost.com", "cnbc.com",
    "aljazeera.com", "scmp.com", "thehindu.com",
    "economictimes.indiatimes.com",
]

TIER_2_DOMAINS = [
    # Gulf / Middle East English press
    "arabnews.com", "gulfnews.com", "thenationalnews.com",
    "khaleejtimes.com", "zawya.com", "menafn.com",
    "albawaba.com", "middleeasteye.net",
    # Energy, oil & gas trade publications
    "oilprice.com", "rigzone.com", "offshore-energy.biz",
    "naturalgasworld.com", "lngworldnews.com",
    "energymonitor.ai", "energyintel.com",
    "spglobal.com", "platts.com",
    # Shipping and logistics trade press
    "supplychaindive.com", "freightwaves.com", "joc.com",
    "tradewindsnews.com", "lloydslist.com",
    "seatrade-maritime.com", "hellenicshippingnews.com",
    "marinetraffic.com",
    # Chemical / petrochemical industry
    "icis.com", "chemweek.com", "echemi.com",
    # General Asia / electronics / manufacturing press (useful default coverage)
    "nikkei.com", "asia.nikkei.com", "channelnewsasia.com",
    "japantimes.co.jp", "koreaherald.com", "yna.co.kr",
]

TRUSTED_DOMAINS = TIER_1_DOMAINS + TIER_2_DOMAINS
TRUSTED_DOMAINS_SET = set(TRUSTED_DOMAINS)

MIN_VIABLE_TOKENS = 60


# ==========================================================================
# DISRUPTION THEME CODES (GDELT GKG V2Themes)
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


# ==========================================================================
# SQL HELPERS
# ==========================================================================
def _sql_escape(s: str) -> str:
    """Escape single quotes for safe inlining into a SQL string literal."""
    return s.replace("'", "''")


def _domains_in_clause() -> str:
    """Build:  SourceCommonName IN ('reuters.com', 'apnews.com', ...)"""
    quoted = ", ".join(f"'{_sql_escape(d)}'" for d in TRUSTED_DOMAINS)
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
#   USER INPUT, not a hardcoded constant — so we validate defensively.
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
# QUERY GENERATION
#   Identical prong logic to the trial script, but `profile` is now
#   whatever the user submitted (validated first).
# ==========================================================================
def generate_queries(profile: dict, domain_filter: bool = True) -> list:
    """
    Returns a list of dicts: {type, anchor, where}
    where `where` is a SQL boolean expression (without the word WHERE).

    domain_filter=True  (default, production): appends
        AND SourceCommonName IN (...) to every query — trusted outlets only.
    domain_filter=False (discover/dev only): no domain constraint in SQL;
        BigQuery returns all matching articles regardless of outlet.
    """
    queries = []
    dom = _domains_in_clause() if domain_filter else None

    def _and_dom():
        return f"AND {dom}" if dom else ""

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


def audit_coverage(profile: dict) -> dict:
    """Returns theme-code counts per disruption domain (for diagnostics/UI)."""
    return {d: len(codes) for d, codes in DOMAIN_THEMES.items()}