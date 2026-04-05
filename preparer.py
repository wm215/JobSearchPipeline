"""
preparer.py — Cover letter + resume summary generator for the JobSearchPipeline.

Stage 3 of the pipeline: takes researched jobs and generates tailored
application materials (cover letter, resume summary) for each.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Optional

from db import get_unprepared, save_application_materials

# ---------------------------------------------------------------------------
# Role type constants
# ---------------------------------------------------------------------------

ROLE_FEDERAL_HOUSING = "federal_housing"
ROLE_GOVCON = "govcon"
ROLE_COMPLIANCE = "compliance"
ROLE_GRANTS = "grants"
ROLE_COMMUNITY_DEVELOPMENT = "community_development"
ROLE_GOVERNMENT = "government"
ROLE_GENERAL = "general"

# ---------------------------------------------------------------------------
# Credential hooks — opening paragraph per role type
# ---------------------------------------------------------------------------

CREDENTIAL_HOOKS: dict[str, str] = {
    ROLE_FEDERAL_HOUSING: (
        "With 15 years at HUD managing REO and Section 8 portfolios, FAC-COR Level 3 "
        "certification, PMP credentials, and direct experience administering CDBG grants "
        "and FHA programs, I offer a rare combination of technical expertise and hands-on "
        "federal housing program management."
    ),
    ROLE_GOVCON: (
        "As a FAC-COR Level 3 certified contracting professional with 10+ federal contracts "
        "managed and oversight of $50M in real estate assets, I bring deep FAR fluency, "
        "acquisition lifecycle experience, and a track record of delivering compliant, "
        "cost-effective government contracts."
    ),
    ROLE_COMPLIANCE: (
        "My compliance background spans 5,000+ properties and yielded a 22% improvement "
        "in compliance rates and a 25% efficiency gain across oversight operations — "
        "results achieved through systematic process redesign, rigorous reporting, "
        "and proactive regulatory engagement."
    ),
    ROLE_GRANTS: (
        "I have administered CDBG and HOME programs, coordinating with 30+ elected officials "
        "and 100+ community organizations to drive equitable resource allocation — "
        "experience that directly maps to the grants management challenges this role demands."
    ),
    ROLE_COMMUNITY_DEVELOPMENT: (
        "My experience in CDBG administration and direct engagement with 100+ community "
        "stakeholders and 30+ elected officials has sharpened my ability to translate "
        "policy into neighborhood-level impact — exactly the work this role calls for."
    ),
    ROLE_GOVERNMENT: (
        "Across 12 congressional offices and partnerships with 30+ elected officials, "
        "I have delivered a 22% compliance improvement and a 25% efficiency gain while "
        "navigating the complexities of municipal and intergovernmental coordination."
    ),
    ROLE_GENERAL: (
        "With 15 years at HUD, PMP certification, FAC-COR Level 3 credentials, and "
        "oversight of $50M in federal contracts and real estate assets, I bring "
        "program management depth that drives measurable results in complex, "
        "stakeholder-intensive environments."
    ),
}

# ---------------------------------------------------------------------------
# Impact paragraphs — body paragraph per role type
# ---------------------------------------------------------------------------

IMPACT_PARAS: dict[str, str] = {
    ROLE_FEDERAL_HOUSING: (
        "Throughout my tenure at HUD, I managed complex REO portfolios, coordinated "
        "Section 8 voucher programs, and served as the primary compliance liaison for "
        "field offices across multiple regions. My work directly supported HUD's mission "
        "to create strong, sustainable, inclusive communities — and I am eager to bring "
        "that institutional knowledge to your organization."
    ),
    ROLE_GOVCON: (
        "I have guided contracts through every phase of the acquisition lifecycle — "
        "from market research and solicitation through award, administration, and closeout. "
        "My FAR expertise and COR certification ensure that oversight is both rigorous "
        "and aligned with agency mission, reducing risk while accelerating delivery."
    ),
    ROLE_COMPLIANCE: (
        "My approach to compliance is data-driven and process-oriented: I build "
        "monitoring frameworks that surface issues early, design corrective action plans "
        "that stick, and train teams to sustain improvements long after initial "
        "implementation. The results speak for themselves — 22% compliance rate improvement "
        "across a portfolio of 5,000+ properties."
    ),
    ROLE_GRANTS: (
        "Effective grants management requires both technical precision and relationship "
        "capital. I have built both: managing multi-million-dollar CDBG and HOME allocations "
        "while maintaining trusted partnerships with grantees, elected officials, and "
        "community advocates who depend on these programs to deliver."
    ),
    ROLE_COMMUNITY_DEVELOPMENT: (
        "Community development work lives at the intersection of policy, finance, "
        "and trust. My background in CDBG administration and stakeholder engagement — "
        "across neighborhood associations, housing nonprofits, and city agencies — "
        "gives me the credibility and technical range to advance complex development goals."
    ),
    ROLE_GOVERNMENT: (
        "Public-sector work demands the ability to navigate political complexity while "
        "delivering tangible results. My experience coordinating across city departments, "
        "congressional offices, and community stakeholders has honed my ability to build "
        "consensus, manage competing priorities, and keep programs on track."
    ),
    ROLE_GENERAL: (
        "I am known for translating strategic priorities into operational results — "
        "whether that means standing up a new compliance framework, renegotiating a "
        "troubled contract, or mobilizing a cross-functional team around a shared goal. "
        "My PMP certification and federal program management background give me the "
        "structure to execute while my stakeholder relationships enable the influence "
        "required to succeed."
    ),
}

# ---------------------------------------------------------------------------
# Resume summaries per role type
# ---------------------------------------------------------------------------

RESUME_SUMMARIES: dict[str, str] = {
    ROLE_FEDERAL_HOUSING: (
        "Senior federal housing program professional with 15 years at HUD managing REO "
        "portfolios, Section 8 programs, and CDBG grant administration. FAC-COR Level 3 "
        "certified and PMP credentialed, with a record of driving compliance improvements "
        "and stakeholder alignment across complex, multi-site programs. Proven ability to "
        "deliver results within federal regulatory frameworks."
    ),
    ROLE_GOVCON: (
        "FAC-COR Level 3 certified acquisition professional with 10+ federal contracts "
        "managed and $50M in real estate asset oversight. Deep FAR fluency across the "
        "full acquisition lifecycle — from market research through closeout — combined with "
        "program management expertise that ensures contracts are delivered on time, "
        "within scope, and fully compliant."
    ),
    ROLE_COMPLIANCE: (
        "Compliance leader with a demonstrated record of driving a 22% improvement in "
        "compliance rates and a 25% efficiency gain across a portfolio of 5,000+ properties. "
        "Skilled in regulatory analysis, corrective action planning, and building monitoring "
        "systems that sustain results. Brings federal program management experience and "
        "PMP certification to high-stakes oversight roles."
    ),
    ROLE_GRANTS: (
        "Grants management professional with direct experience administering CDBG and HOME "
        "programs in partnership with 30+ elected officials and 100+ community organizations. "
        "Combines technical grants compliance expertise with stakeholder relationship skills "
        "to ensure equitable allocation and program integrity. PMP certified with 15 years "
        "of federal program management experience."
    ),
    ROLE_COMMUNITY_DEVELOPMENT: (
        "Community development professional with deep experience in CDBG program "
        "administration, neighborhood planning, and cross-sector stakeholder engagement. "
        "Has partnered with 100+ community organizations and 30+ elected officials to "
        "advance housing and economic development priorities. Brings federal program "
        "management credentials and a track record of translating policy into place-based impact."
    ),
    ROLE_GOVERNMENT: (
        "Public-sector program manager with 15 years of experience coordinating across "
        "municipal agencies, congressional offices, and community stakeholders. Delivered "
        "a 22% compliance improvement and a 25% efficiency gain while managing complex "
        "intergovernmental portfolios. PMP certified with deep expertise in policy "
        "implementation, regulatory compliance, and government relations."
    ),
    ROLE_GENERAL: (
        "Results-driven program manager with 15 years at HUD, PMP certification, and "
        "FAC-COR Level 3 credentials. Managed $50M+ in federal contracts and real estate "
        "assets with a consistent record of compliance improvement, stakeholder alignment, "
        "and operational efficiency gains. Equally effective in strategic planning and "
        "hands-on program execution."
    ),
}

# ---------------------------------------------------------------------------
# Role classifier
# ---------------------------------------------------------------------------

_ROLE_KEYWORDS: list[tuple[str, list[str]]] = [
    (ROLE_FEDERAL_HOUSING, ["hud", "housing authority", "section 8", "cdbg", "fha", "cpd"]),
    (ROLE_GOVCON, ["federal acquisition", "far ", "contracting officer", "fac-cor", "govcon"]),
    (ROLE_COMPLIANCE, ["compliance", "regulatory", "audit", "oversight"]),
    (ROLE_GRANTS, ["grant", "grants management", "federal grants"]),
    (ROLE_COMMUNITY_DEVELOPMENT, ["community development", "community planning", "neighborhood"]),
    (ROLE_GOVERNMENT, ["city of", "county", "state agency", "municipal", "public sector"]),
]


def classify_role(title: str, description: str) -> str:
    """
    Classify a job into one of 7 role types based on title and description keywords.

    Checks lowercase(title + first 400 chars of description) against keyword lists
    in priority order — first match wins.

    Returns one of: "federal_housing", "govcon", "compliance", "grants",
                    "community_development", "government", "general"
    """
    haystack = (title or "").lower() + " " + (description or "")[:400].lower()
    for role_type, keywords in _ROLE_KEYWORDS:
        for kw in keywords:
            if kw in haystack:
                return role_type
    return ROLE_GENERAL


# ---------------------------------------------------------------------------
# Cover letter generator
# ---------------------------------------------------------------------------

_CLOSE = (
    "I'd welcome a direct conversation about how my background maps to your "
    "current priorities."
)

_SIGNATURE = "William A. Melendez\nwmelendez215@gmail.com\nChicago, IL"


def generate_cover_letter(title: str, company: str, description: str) -> str:
    """
    Generate a full cover letter tailored to the job's role type.

    Uses CREDENTIAL_HOOKS and IMPACT_PARAS dicts keyed by classify_role output.
    Returns a multi-paragraph letter with date, Re: line, 3 paragraphs, close,
    and signature.
    """
    role_type = classify_role(title, description)
    hook = CREDENTIAL_HOOKS[role_type]
    impact = IMPACT_PARAS[role_type]
    today = date.today().strftime("%B %d, %Y")

    letter = (
        f"{today}\n\n"
        f"Re: {title} — {company}\n\n"
        f"Dear Hiring Manager,\n\n"
        f"{hook}\n\n"
        f"{impact}\n\n"
        f"I have spent my career at the intersection of federal policy, program management, "
        f"and community impact. {company} represents exactly the kind of mission-driven "
        f"organization where that experience translates directly. I am confident I can "
        f"contribute from day one.\n\n"
        f"{_CLOSE}\n\n"
        f"Sincerely,\n\n"
        f"{_SIGNATURE}"
    )
    return letter


# ---------------------------------------------------------------------------
# Resume summary generator
# ---------------------------------------------------------------------------


def generate_resume_summary(title: str, company: str, description: str) -> str:
    """
    Generate a 3-4 sentence tailored resume summary based on the job's role type.
    """
    role_type = classify_role(title, description)
    return RESUME_SUMMARIES[role_type]


# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------


def run_prepare(conn: sqlite3.Connection) -> int:
    """
    Generate application materials for all researched-but-unprepared jobs.

    Gets unprepared jobs from the DB, generates a cover letter and resume
    summary for each, and saves via save_application_materials.

    Returns the count of jobs prepared.
    """
    jobs = get_unprepared(conn)
    count = 0
    for job in jobs:
        job_id = job["job_id"]
        title = job.get("title", "")
        company = job.get("company", "")
        description = job.get("description", "")

        cover_letter = generate_cover_letter(title, company, description)
        resume_summary = generate_resume_summary(title, company, description)

        save_application_materials(conn, job_id, {
            "resume_tailored": resume_summary,
            "cover_letter": cover_letter,
            "email_draft": "",
            "linkedin_note": "",
        })
        count += 1
    return count
