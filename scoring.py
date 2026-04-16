"""
scoring.py — Qualification-gated job scoring engine for the JobSearchPipeline.

Pipeline:
  1. DISQUALIFY  — role requires a credential Will doesn't have → score 0
  2. SALARY GATE — both min and max must be >= $90k (or missing) → score 0
  3. DOMAIN      — 35 pts, tier-based keyword matching
  4. ROLE        — 25 pts, title keyword matching
  5. SKILLS      — 20 pts, core-skills count from description
  6. SALARY      — 10 pts, based on advertised range
  7. LOCATION    — 10 pts, target-city matching
  8. DOMAIN GATE — if domain < DOMAIN_GATE_MIN, cap total at 50

Decision: AUTO_APPLY >= 75 | REVIEW 55-74 | SKIP < 55
"""

from __future__ import annotations

from config import (
    AUTO_APPLY_THRESHOLD,
    DISQUALIFY_PHRASES,
    DOMAIN_GATE_MIN,
    PRIVATE_SECTOR_CAP,
    REVIEW_THRESHOLD,
)

# ---------------------------------------------------------------------------
# Core skills used for the skills dimension
# ---------------------------------------------------------------------------

CORE_SKILLS: list[str] = [
    "program management",
    "project management",
    "stakeholder",
    "federal",
    "government",
    "housing",
    "policy",
    "compliance",
    "contract",
    "contracting",
    "procurement",
    "grants",
    "far",
    "acquisition",
    "intergovernmental",
    "asset management",
    "property management",
    "real estate",
    "performance management",
    "budget",
    "community development",
    "data analysis",
    "reporting",
    "briefing",
    "liaison",
    "regulatory",
    "oversight",
    "pmp",
    "cor",
    "fac-cor",
]

# ---------------------------------------------------------------------------
# Domain keyword tiers
# ---------------------------------------------------------------------------

_DOMAIN_TIER1_KEYWORDS: list[str] = [
    "hud",
    "department of housing",
    "housing authority",
    "fac-cor",
    "section 8",
    "cdbg",
    "reo",
    "home investment partnership",
    "hope vi",
    "public housing",
    "housing choice voucher",
    "housing finance agency",
    "affordable housing development",
    "low-income housing",
    "lihtc",
    "fair housing",
    "housing and urban development",
]

_DOMAIN_TIER2_KEYWORDS: list[str] = [
    "city of chicago",
    "city of philadelphia",
    "cook county",
    "affordable housing",
    "intergovernmental",
    "community development",
    "community planning",
    "city government",
    "county government",
    "municipal",
    "mayor's office",
    "department of planning",
    "department of community",
    "redevelopment authority",
    "housing department",
    "neighborhood development",
    "urban planning",
]

_DOMAIN_TIER3_KEYWORDS: list[str] = [
    "federal",
    "u.s. department",
    "gs-",
    "guidehouse",
    "booz allen",
    "deloitte federal",
    "grants management",
    "policy",
    "federal agency",
    "government agency",
    "department of",
    "bureau of",
    "office of",
    "u.s. government",
    "hhs",
    "epa",
    "dot",
    "doe",
    "usda",
    "treasury",
    "fema",
    "omb",
    "gao",
    "oversight",
    "regulatory",
    "federal contract",
    "government contract",
    "acquisition",
]

# ---------------------------------------------------------------------------
# LinkedIn bonus data (hardcoded from LinkedIn export, updated Jan 2026)
# ---------------------------------------------------------------------------

# Company pattern → bonus points (max +10).  Substring match on company name.
LINKEDIN_CONNECTION_BONUSES: dict[str, int] = {
    "u.s. department of housing and urban development": 10,
    "hud": 10,
    "city of philadelphia": 8,
    "chicago housing authority": 7,
    "philadelphia housing authority": 7,
    "fannie mae": 6,
    "freddie mac": 6,
    "illinois department of human services": 6,
    "cook county government": 6,
    "illinois department of commerce": 6,
    "guardian asset management": 5,
    "pk management": 5,
    "first allegiance": 5,
    "asset management specialist": 5,
    "safeguard properties": 5,
}

# Skill keyword → bonus points (max +10).  Best single match wins.
LINKEDIN_ENDORSEMENT_BONUSES: dict[str, int] = {
    "reo": 8,
    "hud": 8,
    "foreclosure": 7,
    "property management": 7,
    "real estate": 6,
    "asset management": 6,
    "management": 5,
    "leadership": 5,
    "customer service": 4,
    "microsoft office": 3,
}

# Org pattern → bonus points (max +10).  First substring match wins.
LINKEDIN_APPLICATION_HISTORY: dict[str, int] = {
    "chicago housing authority": 10,
    "u.s. department of housing and urban development": 10,
    "philadelphia housing authority": 9,
    "fannie mae": 9,
    "city of chicago": 8,
    "city of philadelphia": 8,
    "experian": 6,
    "asana": 6,
}


_DOMAIN_TIER4_KEYWORDS: list[str] = [
    "real estate",
    "property management",
    "asset management",
    "workforce development",
    "nonprofit",
    "non-profit",
    "community organization",
    "social services",
    "philanthropy",
    "foundation",
    "public sector",
    "public administration",
    "state government",
    "state agency",
]

# ---------------------------------------------------------------------------
# Role keyword tiers (matched against lowercase title only)
# ---------------------------------------------------------------------------

_ROLE_STRONG_KEYWORDS: list[str] = [
    "program manager",
    "program analyst",
    "program director",
    "policy analyst",
    "policy director",
    "policy manager",
    "contracting officer",
    "grants manager",
    "grants administrator",
    "housing specialist",
    "housing manager",
    "housing director",
    "housing program",
    "contract specialist",
    "compliance manager",
    "compliance director",
    "government affairs",
    "government relations",
    "deputy director",
    "executive director",
    "management analyst",
    "budget analyst",
    "budget manager",
    "acquisition manager",
    "acquisition specialist",
    "inspector general",
    "grants management",
    "program officer",
    "policy officer",
    "program specialist",
]

_ROLE_PARTIAL_KEYWORDS: list[str] = [
    "project manager",
    "business analyst",
    "senior director",
    "procurement analyst",
    "operations manager",
    "senior manager",
    "senior analyst",
    "senior specialist",
    "strategic advisor",
    "senior advisor",
    "portfolio manager",
    "relationship manager",
    "engagement manager",
]

_ROLE_WEAK_KEYWORDS: list[str] = [
    "manager",
    "analyst",
    "coordinator",
    "specialist",
    "advisor",
    "consultant",
    "administrator",
    "director",
    "officer",
]

# Salary threshold constants
_SALARY_GATE: int = 90_000
_SALARY_HIGH: int = 100_000
_SALARY_NEAR: int = 85_000


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_disqualified(title: str, description: str) -> tuple[bool, str | None]:
    """
    Check whether a job should be immediately disqualified.

    Searches lowercase(title) + lowercase(first 600 chars of description)
    for any phrase in DISQUALIFY_PHRASES.

    Returns:
        (True, matched_phrase) if disqualified.
        (False, None)          otherwise.
    """
    haystack = (title or "").lower() + " " + (description or "")[:600].lower()
    for phrase in DISQUALIFY_PHRASES:
        if phrase.lower() in haystack:
            return True, phrase
    return False, None


def score_job(
    title: str,
    description: str,
    company: str,
    location: str,
    salary_min: int | None = None,
    salary_max: int | None = None,
) -> dict:
    """
    Score a job listing 0-100 across 5 dimensions.

    Returns a dict with keys:
        total_score  : int          (0-100)
        status       : str          (DISQUALIFIED | SKIP | REVIEW | AUTO_APPLY)
        reason       : str | None   (set when DISQUALIFIED)
        breakdown    : dict         (per-dimension scores)
    """
    # -----------------------------------------------------------------------
    # Step 1: Hard disqualification
    # -----------------------------------------------------------------------
    disq, phrase = is_disqualified(title, description)
    if disq:
        return _disqualified(f"Disqualify phrase matched: '{phrase}'")

    # -----------------------------------------------------------------------
    # Step 2: Salary gate
    # -----------------------------------------------------------------------
    if salary_min is not None and salary_max is not None:
        # Both values present — both must clear the floor
        if salary_max < _SALARY_GATE:
            return _disqualified(
                f"Salary too low: max ${salary_max:,} < ${_SALARY_GATE:,}"
            )
        if salary_min < _SALARY_GATE and salary_max < _SALARY_GATE:
            return _disqualified(
                f"Salary too low: min ${salary_min:,} and max ${salary_max:,} both < ${_SALARY_GATE:,}"
            )
    elif salary_max is not None and salary_max < _SALARY_GATE:
        # Only max present and it's below threshold
        return _disqualified(
            f"Salary too low: max ${salary_max:,} < ${_SALARY_GATE:,}"
        )

    # -----------------------------------------------------------------------
    # Steps 3-7: Dimension scoring
    # -----------------------------------------------------------------------
    combined = f"{title} {description} {company}".lower()
    title_lower = (title or "").lower()
    company_lower = (company or "").lower()
    location_lower = (location or "").lower()

    domain = _score_domain(combined, company_lower)
    role = _score_role(title_lower)
    skills = _score_skills(description)
    salary = _score_salary(salary_min, salary_max)
    loc = _score_location(location_lower)

    raw_total = domain + role + skills + salary + loc

    # -----------------------------------------------------------------------
    # Step 8: Domain gate
    # -----------------------------------------------------------------------
    reason: str | None = None
    if domain < DOMAIN_GATE_MIN:
        raw_total = min(raw_total, 50)
        reason = f"Domain gate: domain score {domain} < {DOMAIN_GATE_MIN}"

    # -----------------------------------------------------------------------
    # Step 9: LinkedIn bonus (connections + endorsements + app history)
    # -----------------------------------------------------------------------
    conn_bonus, endorse_bonus, app_bonus = _score_linkedin(company_lower, combined)
    linkedin_total = min(conn_bonus + endorse_bonus + app_bonus, 30)
    raw_total += linkedin_total

    total_score = min(100, max(0, raw_total))
    status = _decide(total_score)

    return {
        "total_score": total_score,
        "status": status,
        "reason": reason,
        "breakdown": {
            "domain": domain,
            "role": role,
            "skills": skills,
            "salary": salary,
            "location": loc,
            "linkedin_connections": conn_bonus,
            "linkedin_endorsements": endorse_bonus,
            "linkedin_app_history": app_bonus,
        },
    }


# ---------------------------------------------------------------------------
# Dimension helpers
# ---------------------------------------------------------------------------


def _score_domain(combined_lower: str, company_lower: str) -> int:
    """Score the domain dimension (0-35 pts)."""
    # Private-sector employer cap: max 8 pts regardless of keyword matches
    for cap_company in PRIVATE_SECTOR_CAP:
        if cap_company.lower() in company_lower:
            return 8

    # Tier 1: HUD / housing authority specific (35 pts)
    for kw in _DOMAIN_TIER1_KEYWORDS:
        if kw in combined_lower:
            return 35

    # Tier 2: City / county / state government (25 pts)
    for kw in _DOMAIN_TIER2_KEYWORDS:
        if kw in combined_lower:
            return 25

    # Tier 3: Federal / govcon (17 pts)
    for kw in _DOMAIN_TIER3_KEYWORDS:
        if kw in combined_lower:
            return 17

    # Tier 4: Adjacent (8 pts)
    for kw in _DOMAIN_TIER4_KEYWORDS:
        if kw in combined_lower:
            return 8

    return 0


def _score_role(title_lower: str) -> int:
    """Score the role dimension (0-25 pts)."""
    for kw in _ROLE_STRONG_KEYWORDS:
        if kw in title_lower:
            return 25

    for kw in _ROLE_PARTIAL_KEYWORDS:
        if kw in title_lower:
            return 17

    for kw in _ROLE_WEAK_KEYWORDS:
        if kw in title_lower:
            return 9

    return 0


def _score_skills(description: str) -> int:
    """Score the skills dimension (0-20 pts)."""
    desc_lower = (description or "").lower()
    count = sum(1 for skill in CORE_SKILLS if skill in desc_lower)
    ratio = min(count / 5.0, 1.0)
    return round(ratio * 20)


def _score_salary(salary_min: int | None, salary_max: int | None) -> int:
    """Score the salary dimension (0-10 pts)."""
    if salary_min is not None and salary_min >= _SALARY_HIGH:
        return 10
    if salary_max is not None and salary_max >= _SALARY_HIGH:
        return 7
    if salary_min is None and salary_max is None:
        return 5
    if salary_max is not None and salary_max >= _SALARY_NEAR:
        return 3
    return 0


def _score_linkedin(company_lower: str, combined_lower: str) -> tuple[int, int, int]:
    """
    Score LinkedIn bonuses: connections, endorsements, application history.

    Returns (connection_bonus, endorsement_bonus, app_history_bonus).
    Each is independently capped at 10; the caller caps the sum at 30.
    """
    # Connection bonus — first substring match wins
    conn_bonus = 0
    for pattern, pts in LINKEDIN_CONNECTION_BONUSES.items():
        if pattern in company_lower:
            conn_bonus = pts
            break

    # Endorsement bonus — best single match across title + description
    endorse_bonus = 0
    for skill, pts in LINKEDIN_ENDORSEMENT_BONUSES.items():
        if skill in combined_lower:
            endorse_bonus = max(endorse_bonus, pts)

    # Application history bonus — first substring match wins
    app_bonus = 0
    for pattern, pts in LINKEDIN_APPLICATION_HISTORY.items():
        if pattern in company_lower:
            app_bonus = pts
            break

    return conn_bonus, endorse_bonus, app_bonus


def _score_location(location_lower: str) -> int:
    """Score the location dimension (0-10 pts)."""
    if "philadelphia" in location_lower or "philly" in location_lower:
        return 10
    if "new jersey" in location_lower or ", nj" in location_lower:
        return 10
    if "delaware" in location_lower or ", de" in location_lower:
        return 10
    if "washington" in location_lower and ("dc" in location_lower or "d.c." in location_lower):
        return 8
    if "chicago" in location_lower:
        return 7
    if "remote" in location_lower:
        return 6
    if "telework" in location_lower:
        return 6
    if "multiple locations" in location_lower:
        return 5
    if "location negotiable" in location_lower:
        return 4
    return 0


# ---------------------------------------------------------------------------
# Decision and helper
# ---------------------------------------------------------------------------


def _decide(score: int) -> str:
    if score >= AUTO_APPLY_THRESHOLD:
        return "AUTO_APPLY"
    if score >= REVIEW_THRESHOLD:
        return "REVIEW"
    return "SKIP"


def _disqualified(reason: str) -> dict:
    return {
        "total_score": 0,
        "status": "DISQUALIFIED",
        "reason": reason,
        "breakdown": {
            "domain": 0,
            "role": 0,
            "skills": 0,
            "salary": 0,
            "location": 0,
        },
    }
