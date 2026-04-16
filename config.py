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
APIFY_API_TOKEN: str = os.getenv("APIFY_API_TOKEN", os.getenv("APIFY_TOKEN", ""))

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
    "locations": [
        "Philadelphia PA",
        "New Jersey",
        "Delaware",
        "Washington DC",
        "Chicago IL",
    ],
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

AUTO_APPLY_THRESHOLD: int = 75   # score >= this → auto-apply
REVIEW_THRESHOLD: int = 55       # score >= this → human review
DOMAIN_GATE_MIN: int = 10        # minimum domain score to pass gate

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
# Indeed / Apify
# ---------------------------------------------------------------------------

INDEED_ACTOR: str = "valig/indeed-jobs-scraper"

INDEED_KEYWORDS: list[str] = [
    "Program Analyst",
    "Program Manager",
    "Contract Specialist",
    "Government Affairs",
    "Policy Director",
    "Housing Director",
    "Compliance Manager",
    "Government Relations",
]

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
    "blackrock",
    "vanguard",
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
