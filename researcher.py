"""
researcher.py — Agency research + fit summary generator for the JobSearchPipeline.

Stage 2 of the pipeline: enriches high-scoring unresearched jobs with agency
context and William's relevant qualifications.
"""

from __future__ import annotations

import sqlite3

from db import get_top_unresearched, save_research

# ---------------------------------------------------------------------------
# Known agency data
# ---------------------------------------------------------------------------

KNOWN_AGENCIES: dict[str, dict] = {
    "hud": {
        "mission": (
            "The U.S. Department of Housing and Urban Development creates strong, "
            "sustainable, inclusive communities and quality affordable homes for all. "
            "HUD works to strengthen the housing market, bolster the economy and protect "
            "consumers, meet the need for quality affordable rental homes, utilize housing "
            "as a platform for improving quality of life, and build inclusive and "
            "sustainable communities."
        ),
        "org_size": "~8,000 employees",
    },
    "cbp": {
        "mission": (
            "U.S. Customs and Border Protection (CBP) is one of the world's largest law "
            "enforcement organizations and is charged with keeping terrorists and their "
            "weapons out of the U.S. while facilitating lawful international travel and trade."
        ),
        "org_size": "~60,000 employees",
    },
    "doe": {
        "mission": (
            "The U.S. Department of Energy ensures America's security and prosperity by "
            "addressing its energy, environmental, and nuclear challenges through "
            "transformative science and technology solutions."
        ),
        "org_size": "~14,000 employees",
    },
    "treasury": {
        "mission": (
            "The U.S. Department of the Treasury maintains a strong economy and creates "
            "economic and job opportunities by promoting the conditions that enable economic "
            "growth and stability at home and abroad, strengthen national security by "
            "combating threats and protecting the integrity of the financial system, and "
            "manage the U.S. government's finances and resources effectively."
        ),
        "org_size": "~100,000 employees",
    },
    "cha": {
        "mission": (
            "The Chicago Housing Authority (CHA) provides quality housing in healthy, "
            "vibrant communities for more than 50,000 Chicago families by developing and "
            "managing affordable housing and creating pathways to self-sufficiency for "
            "residents. CHA administers the Housing Choice Voucher program and manages "
            "public housing developments across Chicago."
        ),
        "org_size": "~1,000 employees",
    },
    "city of philadelphia": {
        "mission": (
            "The City of Philadelphia serves its residents and businesses by providing "
            "municipal services, maintaining infrastructure, and advancing the city's "
            "economic development, public safety, and quality of life. Philadelphia's "
            "government is committed to equity, transparency, and community engagement "
            "in all its operations."
        ),
        "org_size": "~30,000 employees",
    },
    "city of chicago": {
        "mission": (
            "The City of Chicago serves its 2.7 million residents through a broad range "
            "of municipal services and agencies committed to delivering equitable, "
            "high-quality city services. Chicago's city government works to promote "
            "economic opportunity, neighborhood investment, public safety, and community "
            "development across all 77 neighborhoods."
        ),
        "org_size": "~33,000 employees",
    },
    "fannie mae": {
        "mission": (
            "Fannie Mae serves the nation by providing reliable, large-scale access to "
            "affordable mortgage credit. As a government-sponsored enterprise, Fannie Mae "
            "facilitates the flow of funds in the secondary mortgage market, making "
            "homeownership possible for millions of Americans and helping to stabilize "
            "housing markets across the country."
        ),
        "org_size": "~8,000 employees",
    },
}

# ---------------------------------------------------------------------------
# Fit summary keywords → qualification strings
# ---------------------------------------------------------------------------

_FIT_KEYWORDS: list[tuple[str, str]] = [
    ("compliance", "Compliance track record: 22% improvement across 5,000+ properties"),
    ("housing", "15 years at HUD — REO, Section 8, CDBG, FHA program management"),
    ("hud", "15 years at HUD — deep institutional knowledge and relationships"),
    ("section 8", "Section 8/Housing Choice Voucher expertise from HUD tenure"),
    ("cdbg", "Direct CDBG administration and compliance experience"),
    ("contract", "FAC-COR Level 3: 10+ federal contracts managed end-to-end"),
    ("acquisition", "FAC-COR Level 3: full acquisition lifecycle expertise"),
    ("far", "Deep FAR fluency — solicitation through closeout"),
    ("grant", "CDBG and HOME grants administration — 100+ community partners"),
    ("community development", "Community development: 100+ stakeholders, 30+ elected officials"),
    ("stakeholder", "Stakeholder coordination: 12 congressional offices, 30+ officials"),
    ("program management", "PMP certified program manager with $50M+ portfolio oversight"),
    ("policy", "Policy development and implementation: federal and local government"),
    ("budget", "Budget analysis and financial oversight across multi-year federal programs"),
    ("oversight", "Federal oversight expertise: 22% compliance improvement at HUD"),
    ("asset management", "$50M real estate asset management — REO and federal housing"),
    ("property management", "5,000+ properties managed — compliance, asset disposition, reporting"),
]


def _generate_fit_summary(title: str, company: str, description: str) -> str:
    """
    Build a pipe-separated string of William's relevant qualifications.

    Checks description + title + company for fit keywords and appends
    the matching qualification string for each match found.
    """
    haystack = f"{title} {company} {description}".lower()
    matched: list[str] = []
    seen: set[str] = set()
    for keyword, qualification in _FIT_KEYWORDS:
        if keyword in haystack and qualification not in seen:
            matched.append(qualification)
            seen.add(qualification)
    if not matched:
        return "Strong program management background with 15 years of federal experience"
    return " | ".join(matched)


def research_company(company: str, title: str, description: str) -> dict:
    """
    Return a research dict for a given company, title, and description.

    For known agencies: returns mission statement and generated talking_points.
    For unknown agencies: returns placeholder mission and generated talking_points.

    Keys returned: agency_mission, key_challenges, culture_signals, talking_points
    """
    company_lower = (company or "").lower().strip()

    agency_data = None
    for agency_key, data in KNOWN_AGENCIES.items():
        if agency_key in company_lower or company_lower in agency_key:
            agency_data = data
            break

    if agency_data:
        mission = agency_data["mission"]
    else:
        mission = (
            f"{company} operates in the public-sector or government-adjacent space. "
            "Full mission details should be reviewed on the organization's official website "
            "before the application or interview process."
        )

    talking_points = _generate_fit_summary(title, company, description)

    return {
        "agency_mission": mission,
        "key_challenges": "",
        "culture_signals": "",
        "talking_points": talking_points,
    }


# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------


def run_research(conn: sqlite3.Connection) -> int:
    """
    Research all high-scoring unresearched jobs in the pipeline.

    Gets jobs with match_score >= 75 that have no research row, calls
    research_company for each, and saves via save_research.

    Returns the count of jobs researched.
    """
    jobs = get_top_unresearched(conn)
    count = 0
    for job in jobs:
        research = research_company(
            job.get("company", ""),
            job.get("title", ""),
            job.get("description", ""),
        )
        save_research(conn, job["job_id"], research)
        count += 1
    return count
