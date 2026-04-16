"""
tests/test_scoring.py — Unit tests for the qualification-gated scoring engine.
"""

import pytest
import sys
import os

# Ensure the project root is on sys.path so `from scoring import ...` works
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scoring import is_disqualified, score_job


# ---------------------------------------------------------------------------
# Disqualification tests
# ---------------------------------------------------------------------------


def test_disqualifies_attorney():
    disq, reason = is_disqualified(
        "Assistant United States Attorney", "Legal role requiring JD"
    )
    assert disq is True
    assert "attorney" in reason


def test_disqualifies_warehouse_worker():
    disq, reason = is_disqualified(
        "Materials Handler (Warehouse Worker)", "Moving supplies"
    )
    assert disq is True


def test_does_not_disqualify_program_analyst():
    disq, reason = is_disqualified(
        "Program Analyst", "Federal program management at HUD"
    )
    assert disq is False
    assert reason is None


def test_disqualifies_software_engineer():
    disq, reason = is_disqualified(
        "Software Engineer III", "Full stack web application development"
    )
    assert disq is True


def test_disqualifies_registered_nurse():
    disq, reason = is_disqualified(
        "Registered Nurse - ICU", "Patient care at bedside in ICU unit"
    )
    assert disq is True


def test_disqualifies_cdl_in_description():
    """Disqualify if CDL appears in first 600 chars of description."""
    disq, reason = is_disqualified(
        "Operations Coordinator",
        "Requires CDL class A license for route management.",
    )
    assert disq is True


def test_does_not_disqualify_contract_specialist():
    disq, reason = is_disqualified(
        "Contract Specialist",
        "Federal acquisition, FAR compliance, contracting officer support",
    )
    assert disq is False


# ---------------------------------------------------------------------------
# Salary gate tests
# ---------------------------------------------------------------------------


def test_skip_for_low_salary():
    result = score_job(
        title="Program Analyst",
        description="Federal program management",
        company="HUD",
        location="Chicago, IL",
        salary_min=60000,
        salary_max=85000,
    )
    assert result["total_score"] == 0
    assert result["status"] == "DISQUALIFIED"


def test_skip_when_max_below_90k():
    result = score_job(
        title="Program Analyst",
        description="Policy analysis and stakeholder engagement",
        company="City of Chicago",
        location="Chicago, IL",
        salary_min=70000,
        salary_max=88000,
    )
    assert result["total_score"] == 0
    assert result["status"] == "DISQUALIFIED"


def test_passes_salary_gate_when_max_gte_90k():
    result = score_job(
        title="Program Analyst",
        description="Housing authority program management",
        company="HUD",
        location="Chicago, IL",
        salary_min=85000,
        salary_max=95000,
    )
    assert result["status"] != "DISQUALIFIED"


# ---------------------------------------------------------------------------
# Domain gate tests
# ---------------------------------------------------------------------------


def test_domain_gate_caps_irrelevant_job():
    result = score_job(
        title="Senior Manager",
        description="Lead team operations for retail fulfillment optimization",
        company="Amazon",
        location="Chicago, IL",
        salary_min=120000,
        salary_max=180000,
    )
    assert result["total_score"] <= 50


def test_domain_gate_caps_generic_tech_job():
    result = score_job(
        title="Senior Manager",
        description="Lead operations team for e-commerce platform growth strategy",
        company="Generic Corp",
        location="Chicago, IL",
        salary_min=120000,
        salary_max=160000,
    )
    assert result["total_score"] <= 50


# ---------------------------------------------------------------------------
# Full scoring tests
# ---------------------------------------------------------------------------


def test_scores_perfect_hud_job():
    result = score_job(
        title="Program Analyst",
        description=(
            "HUD field office program management, CDBG grants, stakeholder liaison, "
            "compliance oversight, federal policy, procurement and contracting support"
        ),
        company="U.S. Department of Housing and Urban Development",
        location="Chicago, IL",
        salary_min=99000,
        salary_max=129000,
    )
    assert result["total_score"] >= 85
    assert result["status"] == "AUTO_APPLY"


def test_contract_specialist_scores_well():
    result = score_job(
        title="Contract Specialist",
        description=(
            "Federal acquisition, FAR compliance, contracting officer support, "
            "procurement, government policy, oversight and regulatory management"
        ),
        company="Customs and Border Protection",
        location="Washington, DC",
        salary_min=100000,
        salary_max=140000,
    )
    assert result["total_score"] >= 70
    assert result["status"] in ("AUTO_APPLY", "REVIEW")


def test_city_of_chicago_housing_scores_high():
    result = score_job(
        title="Housing Program Manager",
        description=(
            "Manage affordable housing programs for the City of Chicago. "
            "Stakeholder engagement, compliance, community development, budget oversight."
        ),
        company="City of Chicago",
        location="Chicago, IL",
        salary_min=95000,
        salary_max=125000,
    )
    assert result["total_score"] >= 75
    assert result["status"] == "AUTO_APPLY"


def test_no_salary_listed_gets_neutral_score():
    result = score_job(
        title="Program Analyst",
        description="Federal housing policy and grants management",
        company="HUD",
        location="Washington, DC",
        salary_min=None,
        salary_max=None,
    )
    # Should not be disqualified and salary component should be 5
    assert result["status"] != "DISQUALIFIED"
    assert result["breakdown"]["salary"] == 5


def test_remote_job_gets_reduced_location_score():
    result = score_job(
        title="Policy Analyst",
        description="Federal policy analysis, stakeholder engagement, compliance",
        company="Federal Agency",
        location="Remote",
        salary_min=100000,
        salary_max=130000,
    )
    assert result["breakdown"]["location"] == 6


def test_washington_dc_gets_location_score():
    result = score_job(
        title="Program Analyst",
        description="Federal housing program management",
        company="HUD",
        location="Washington, DC",
        salary_min=100000,
        salary_max=130000,
    )
    assert result["breakdown"]["location"] == 8


def test_non_target_location_gets_default_score():
    result = score_job(
        title="Program Analyst",
        description="Federal housing program management",
        company="HUD",
        location="Denver, CO",
        salary_min=100000,
        salary_max=130000,
    )
    assert result["breakdown"]["location"] == 0


def test_multiple_locations_gets_partial_location_score():
    result = score_job(
        title="Program Analyst",
        description="Federal housing program management",
        company="HUD",
        location="Multiple Locations",
        salary_min=100000,
        salary_max=130000,
    )
    assert result["breakdown"]["location"] == 5


def test_breakdown_keys_present():
    result = score_job(
        title="Program Analyst",
        description="Federal housing program management",
        company="HUD",
        location="Chicago, IL",
        salary_min=100000,
        salary_max=130000,
    )
    for key in ("domain", "role", "skills", "salary", "location",
                "linkedin_connections", "linkedin_endorsements", "linkedin_app_history"):
        assert key in result["breakdown"]


def test_total_score_does_not_exceed_100():
    result = score_job(
        title="Program Analyst GS-13",
        description=(
            "HUD housing authority, section 8, CDBG, reo, fac-cor, federal, "
            "program management, stakeholder, policy, compliance, contracting, "
            "procurement, grants, acquisition, community development, budget"
        ),
        company="U.S. Department of Housing and Urban Development",
        location="Chicago, IL",
        salary_min=110000,
        salary_max=150000,
    )
    assert result["total_score"] <= 100


def test_private_sector_cap_limits_domain():
    """Google job should have domain capped at 8 even with government keywords."""
    result = score_job(
        title="Policy Manager",
        description=(
            "Federal government relations, policy analysis, stakeholder engagement, "
            "program management, compliance, procurement, grants, housing policy"
        ),
        company="Google",
        location="Washington, DC",
        salary_min=150000,
        salary_max=200000,
    )
    assert result["breakdown"]["domain"] == 8


def test_skip_status_for_weak_match():
    result = score_job(
        title="Coordinator",
        description="General coordination and scheduling for office operations",
        company="Small Local Company",
        location="Springfield, IL",
        salary_min=90000,
        salary_max=95000,
    )
    assert result["status"] == "SKIP"


# ---------------------------------------------------------------------------
# LinkedIn bonus tests
# ---------------------------------------------------------------------------


def test_linkedin_connection_bonus_hud():
    """HUD jobs should get +10 connection bonus."""
    result = score_job(
        title="Program Analyst",
        description="Federal program management",
        company="U.S. Department of Housing and Urban Development",
        location="Washington, DC",
        salary_min=100000,
        salary_max=130000,
    )
    assert result["breakdown"]["linkedin_connections"] == 10


def test_linkedin_endorsement_bonus():
    """Jobs mentioning endorsed skills should get endorsement bonus."""
    result = score_job(
        title="REO Specialist",
        description="REO property management and foreclosure disposition",
        company="Some Agency",
        location="Philadelphia, PA",
        salary_min=95000,
        salary_max=120000,
    )
    # "reo" = 8 pts (highest match wins)
    assert result["breakdown"]["linkedin_endorsements"] == 8


def test_linkedin_app_history_bonus():
    """Jobs at orgs with prior applications should get history bonus."""
    result = score_job(
        title="Housing Manager",
        description="Affordable housing program management",
        company="Chicago Housing Authority",
        location="Chicago, IL",
        salary_min=95000,
        salary_max=125000,
    )
    assert result["breakdown"]["linkedin_app_history"] == 10
    assert result["breakdown"]["linkedin_connections"] == 7


def test_linkedin_bonus_capped_at_30():
    """Total LinkedIn bonus should not exceed 30 points."""
    result = score_job(
        title="Program Analyst",
        description=(
            "HUD field office REO foreclosure asset management property management "
            "program management stakeholder compliance federal policy procurement"
        ),
        company="U.S. Department of Housing and Urban Development",
        location="Philadelphia, PA",
        salary_min=110000,
        salary_max=150000,
    )
    bd = result["breakdown"]
    linkedin_sum = bd["linkedin_connections"] + bd["linkedin_endorsements"] + bd["linkedin_app_history"]
    # Individual values: conn=10, endorse=8 (reo), app_history=10 → 28, under cap
    assert linkedin_sum <= 30
    assert result["total_score"] <= 100


def test_no_linkedin_bonus_for_unknown_company():
    """Companies without LinkedIn data should get 0 bonus."""
    result = score_job(
        title="Analyst",
        description="General analysis work",
        company="Random Unknown Corp",
        location="Denver, CO",
        salary_min=95000,
        salary_max=110000,
    )
    assert result["breakdown"]["linkedin_connections"] == 0
    assert result["breakdown"]["linkedin_endorsements"] == 0
    assert result["breakdown"]["linkedin_app_history"] == 0
