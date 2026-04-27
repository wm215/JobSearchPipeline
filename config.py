"""
config.py — Single source of truth for the JobSearchPipeline.

Loads environment variables from ~/.env via python-dotenv and exposes all
profile data, source configuration, and thresholds used by every module.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

load_dotenv(Path.home() / ".env")

EMAIL_USER: str = os.getenv("EMAIL_USER", os.getenv("EMAIL_FROM", os.getenv("SENDER_EMAIL", "")))
EMAIL_PASS: str = os.getenv("EMAIL_PASS", os.getenv("EMAIL_PASSWORD", os.getenv("SENDER_PASSWORD", "")))
EMAIL_TO: str = os.getenv("EMAIL_TO", "")

USAJOBS_API_KEY: str = os.getenv("USAJOBS_API_KEY", "")
USAJOBS_EMAIL: str = EMAIL_USER  # USAJobs uses the same email as the account identifier

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# Keep pipeline data/logs colocated with the active repository checkout.
PIPELINE_DIR: Path = Path(__file__).resolve().parent
DB_PATH: Path = PIPELINE_DIR / "data" / "job_pipeline.db"
LOG_PATH: Path = PIPELINE_DIR / "data" / "pipeline.log"
ERROR_LOG_PATH: Path = PIPELINE_DIR / "data" / "pipeline_errors.log"
DATA_DIR: Path = PIPELINE_DIR / "data"
# Legacy external script paths — kept for reference but no longer used by run.py.
# All scripts are now consolidated into this repo as digest.py, followups.py, tracker.py.
DAILY_ACTION_DIGEST_SCRIPT: Path = PIPELINE_DIR / "digest.py"
FOLLOW_UP_REMINDERS_SCRIPT: Path = PIPELINE_DIR / "followups.py"
GMAIL_TRACKER_SCRIPT: Path = PIPELINE_DIR / "tracker.py"
USAJOBS_SAVED_JOBS_SNAPSHOT: Path = DATA_DIR / "usajobs_saved_jobs_snapshot.txt"
PHA_CAREERS_SNAPSHOT: Path = DATA_DIR / "pha_careers_snapshot.txt"

# ---------------------------------------------------------------------------
# Philadelphia portal URLs (scraped alongside USAJobs/Indeed each pipeline run)
# ---------------------------------------------------------------------------

SMARTRECRUITERS_URL: str = "https://api.smartrecruiters.com/v1/companies/CityofPhiladelphia/postings"
PHDC_CAREERS_URL: str = "https://phdcphila.org/about/careers/"

# ---------------------------------------------------------------------------
# Targeting strategy
# ---------------------------------------------------------------------------

# Primary location priority order for search loops.
TARGET_REGIONS: list[str] = [
    "Philadelphia PA",
    "New Jersey",
    "Delaware",
    "Chicago IL",
]

# DC is enabled by default but gated by salary/score at the scoring layer —
# only "slam-dunk" DC jobs ($130k+ salary + strong role/domain fit) make TOP_MATCH.
INCLUDE_WASHINGTON_DC: bool = os.getenv("INCLUDE_WASHINGTON_DC", "1") == "1"

# Candidate scoring + apply queue thresholds.
APPLY_NOW_THRESHOLD: int = int(os.getenv("APPLY_NOW_THRESHOLD", "75"))

# OpenAI extraction throttling for tracker.py to avoid quota/rate-limit failures.
OPENAI_MAX_CALLS_PER_RUN: int = int(os.getenv("OPENAI_MAX_CALLS_PER_RUN", "25"))
OPENAI_MIN_SECONDS_BETWEEN_CALLS: float = float(os.getenv("OPENAI_MIN_SECONDS_BETWEEN_CALLS", "1.5"))
OPENAI_MAX_RETRIES: int = int(os.getenv("OPENAI_MAX_RETRIES", "2"))
OPENAI_BASE_BACKOFF_SECONDS: float = float(os.getenv("OPENAI_BASE_BACKOFF_SECONDS", "2.0"))

# Attempt to refresh the USAJobs saved-jobs snapshot from clipboard text.
AUTO_REFRESH_SAVED_JOBS_FROM_CLIPBOARD: bool = os.getenv(
    "AUTO_REFRESH_SAVED_JOBS_FROM_CLIPBOARD",
    "1",
) == "1"
AUTO_REFRESH_MIN_SAVED_ROWS: int = int(os.getenv("AUTO_REFRESH_MIN_SAVED_ROWS", "3"))

# Ensure data dir exists at import time
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Candidate profile
# ---------------------------------------------------------------------------

PROFILE: dict = {
    "name": "William Melendez",
    "email": EMAIL_USER,
    "current_title": "Program Analyst",
    "experience_years": 10,
    "locations": TARGET_REGIONS + (["Washington DC"] if INCLUDE_WASHINGTON_DC else []),
    "salary_min": 90_000,
    "salary_max": 200_000,
    "resume_metrics": {
        "portfolio_value_managed": "$2B+",
        "hud_programs": ["REO", "FHA", "Section 8", "CDBG"],
        "clearance": "Public Trust",
        "education": "MPA",
        "certifications": ["PMP", "FAC-COR"],
    },
}

# ---------------------------------------------------------------------------
# Scoring thresholds
# ---------------------------------------------------------------------------

TOP_MATCH_THRESHOLD: int = 75    # score >= this → surface as TOP_MATCH (manual apply)
REVIEW_THRESHOLD: int = 55       # score >= this → human review
DOMAIN_GATE_MIN: int = 15        # minimum domain score to pass gate (Tier 3 federal=17 passes; Tier 4 alone=8 fails)

# ---------------------------------------------------------------------------
# USAJobs occupational series
# ---------------------------------------------------------------------------

USAJOBS_SERIES: list[str] = [
    "0340",  # Program Management
    "0343",  # Management and Program Analysis
    "0301",  # Miscellaneous Administration
    "1102",  # Contracting
    "1101",  # General Business and Industry
    "0201",  # Human Resources Management
    "1170",  # Realty
    "0501",  # Financial Administration
    "1150",  # Industrial Specialist
    "0560",  # Budget Analysis
    "0345",  # Program Support Clerical and Assistance
    "1515",  # Operations Research
    "0110",  # Economist
]

# ---------------------------------------------------------------------------
# Indeed / LinkedIn (via python-jobspy)
# ---------------------------------------------------------------------------

INDEED_KEYWORDS: list[str] = [
    # Original 8 — government / housing / policy core
    "Program Analyst",
    "Program Manager",
    "Contract Specialist",
    "Government Affairs",
    "Policy Director",
    "Housing Director",
    "Compliance Manager",
    "Government Relations",
    # Expansion 2026-04-27 — surface state/county/contractor/nonprofit jobs
    # State / county / municipal
    "State Program Manager",
    "Director of Housing",
    "Housing Authority",
    "Public Housing",
    # Federal contractor
    "Government Contractor",
    "Federal Compliance",
    "Federal Program Manager",
    "GRC Program Manager",
    # GSE / mortgage
    "Mortgage Compliance",
    "Loss Mitigation Manager",
    # Nonprofit / foundation / think tank
    "Foundation Program Officer",
    "Senior Program Officer",
    "Director of Programs",
    "Policy Manager",
]

# ---------------------------------------------------------------------------
# python-jobspy (replaces Apify for Indeed + adds LinkedIn scraping)
# ---------------------------------------------------------------------------

JOBSPY_SITES: list[str] = ["indeed", "linkedin"]
JOBSPY_RESULTS_WANTED: int = 30      # per keyword/location search (lowered 04-27 after keyword expansion)
JOBSPY_HOURS_OLD: int = 168          # 7 days
JOBSPY_COUNTRY: str = "USA"

# ---------------------------------------------------------------------------
# Disqualification phrases (case-insensitive substring match on job title)
# ---------------------------------------------------------------------------

DISQUALIFY_PHRASES: list[str] = [
    # Engineering
    "mechanical engineer",
    "electrical engineer",
    "civil engineer",
    "structural engineer",
    "aerospace engineer",
    "chemical engineer",
    "mep engineer",
    "mep director",
    # Software / Tech
    "software engineer",
    "software developer",
    "full stack",
    "data engineer",
    "machine learning",
    "devops engineer",
    "cloud engineer",
    "network engineer",
    # Healthcare / Medical
    "registered nurse",
    "licensed practical nurse",
    "physician",
    "medical doctor",
    "pharmacist",
    "physical therapist",
    "occupational therapist",
    "clinical director",
    "bedside",
    "patient care technician",
    # Finance / Investments
    "investment banking",
    "portfolio manager",
    "equity analyst",
    "hedge fund",
    "wealth management advisor",
    "financial advisor",
    "mortgage banker",
    "loan officer",
    # Logistics / Supply Chain
    "supply chain manager",
    "warehouse manager",
    "logistics manager",
    "fleet manager",
    "fulfillment manager",
    "materials handler",
    "warehouse worker",
    "last mile launch",
    "last mile operations",
    # Environmental / Safety
    "environmental health and safety",
    "ehs manager",
    "ehs director",
    "osha compliance officer",
    # Legal
    "attorney",
    "lawyer",
    "associate counsel",
    "general counsel",
    "paralegal",
    "legal counsel",
    # Academia
    "assistant professor",
    "associate professor",
    "faculty member",
    "adjunct professor",
    "lecturer",
    "k-12",
    # Sales / Commission
    "sales quota",
    "revenue quota",
    "commission-based",
    # Food / Hospitality
    "head chef",
    "sous chef",
    "line cook",
    "restaurant manager",
    # Public Safety / Trades
    "wildland firefighter",
    "firefighter",
    "correctional officer",
    "police officer",
    "border patrol agent",
    "aircraft pilot",
    "truck driver",
    "cdl",
    "equipment operator",
    # Construction / Skilled Trades — William has no construction experience
    "construction project manager",
    "construction superintendent",
    "construction manager",
    "construction supervisor",
    "construction foreman",
    "general contractor",
    "site superintendent",
    "field engineer",
    "estimator",
    "carpenter",
    "electrician",
    "plumber",
    "hvac technician",
    "welder",
    "ironworker",
    # Architecture (building) — William is a Program Analyst, not a designer
    "architect",
    "architectural designer",
    "draftsman",
    "drafter",
    "interior designer",
    "landscape architect",
    "urban designer",
    # Clinical / Caregiver
    "social worker",
    "case manager",  # often a clinical/social-services role
    "child welfare",
    "behavioral health",
    "substance abuse counselor",
    "therapist",
    # Manufacturing / Production
    "production supervisor",
    "production manager",
    "manufacturing manager",
    "plant manager",
    "machine operator",
    "assembly",
    "quality assurance technician",
    # Retail / Service
    "store manager",
    "retail manager",
    "customer service representative",
    "cashier",
    "barista",
    "server",
    # Misc roles unrelated to William's profile
    "marketing manager",
    "creative director",
    "brand manager",
    "event coordinator",
    "graphic designer",
    "video editor",
    "photographer",
    "executive assistant",  # admin support, not the analyst he is
    "personal assistant",
    "receptionist",
]

# ---------------------------------------------------------------------------
# Private-sector companies that are capped / deprioritized
# ---------------------------------------------------------------------------

PRIVATE_SECTOR_CAP: list[str] = [
    # Big Tech
    "amazon",
    "google",
    "microsoft",
    "apple",
    "meta",
    "facebook",
    "netflix",
    "uber",
    "lyft",
    "airbnb",
    "salesforce",
    "oracle",
    "ibm",
    "intel",
    "nvidia",
    # Retail
    "walmart",
    "target",
    "costco",
    "kroger",
    "walgreens",
    "cvs",
    # Finance
    "jpmorgan",
    "goldman sachs",
    "morgan stanley",
    "bank of america",
    "wells fargo",
    "citibank",
    "citigroup",
    "blackrock",
    "vanguard",
    "bmo",
    "pnc financial",
    "pnc bank",
    "capital one",
    "td bank",
    "td ameritrade",
    "hsbc",
    "american express",
    "amex",
    "discover financial",
    "ally financial",
    "u.s. bank",
    "us bank",
    "fifth third",
    "truist",
    "keybank",
    "charles schwab",
    "state street",
    "bny mellon",
    "northern trust",
    "raymond james",
    "edward jones",
    "fidelity investments",
    "deutsche bank",
    "barclays",
    "ubs",
    "credit suisse",
    "prudential",
    "metlife",
    "aig",
    "allstate",
    "geico",
    "progressive insurance",
    "state farm",
    "nationwide insurance",
    "liberty mutual",
    # Fitness
    "edge fitness",
    "planet fitness",
    "la fitness",
    "equinox",
    # Hospitality
    "marriott",
    "hilton",
    "hyatt",
    # Food / Retail
    "mcdonald's",
    "starbucks",
    "home depot",
    "lowe's",
    "best buy",
    "dollar general",
]
