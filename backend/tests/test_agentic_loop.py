"""Tests for the multi-pass verification loop and its supporting logic.

These cover the behaviours the rebuild exists to fix:
  * the pipeline no longer gives up after one failed query plan
  * catalog-only metadata is not treated as evidence
  * confidence is capped when evidence doesn't earn it
  * queries are never repeated across iterations
"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from services.critic import deterministic_gate, is_data_bearing, is_metadata_only
from services.census_resolver import resolve_census, supported_concepts
from services.verification_service import VerificationService
from services.orchestration import _normalize_year, _build_tier1_tasks


# ---------------------------------------------------------------- critic gate

def test_data_bearing_detection():
    assert is_data_bearing({"data_value": 790895.0}) is True
    assert is_data_bearing({"data_value": 0}) is True  # zero is a real value
    assert is_data_bearing({"snippet": "some text"}) is False
    assert is_data_bearing({"data_value": None}) is False


def test_metadata_only_detection():
    catalog = {"url": "https://catalog.data.gov/dataset/federal-spending", "snippet": "A dataset."}
    real = {"url": "https://apps.bea.gov/api/data?x=1", "data_value": 5.0}
    assert is_metadata_only(catalog) is True
    assert is_metadata_only(real) is False


def test_deterministic_gate_flags_catalog_only_results():
    """The exact failure mode users reported: only stale catalog pages came back."""
    sources = [
        {"url": "https://catalog.data.gov/dataset/a", "snippet": "Dataset A"},
        {"url": "https://catalog.data.gov/dataset/b", "snippet": "Dataset B"},
    ]
    gate = deterministic_gate(sources)
    assert gate["has_hard_evidence"] is False
    assert gate["only_metadata"] is True
    assert gate["data_bearing_count"] == 0


def test_deterministic_gate_accepts_real_datapoint():
    sources = [
        {"url": "https://apps.bea.gov/api/data", "data_value": 790895.0},
        {"url": "https://catalog.data.gov/dataset/a", "snippet": "Dataset A"},
    ]
    gate = deterministic_gate(sources)
    assert gate["has_hard_evidence"] is True
    assert gate["only_metadata"] is False


def test_gate_ignores_error_entries():
    sources = [{"error": "boom", "source": "BEA", "status": "failed"}]
    gate = deterministic_gate(sources)
    assert gate["total_sources"] == 0
    assert gate["has_hard_evidence"] is False


# ------------------------------------------------------------ census resolver

def test_census_resolver_maps_concept_and_state():
    out = resolve_census("median_household_income", "Texas", "2022")
    assert out["for"] == "state:48"
    assert out["get"] == "NAME,B19013_001E"
    assert out["year"] == "2022"


def test_census_resolver_handles_aliases_and_national():
    out = resolve_census("population", "US", "2022")
    assert out["for"] == "us:1"
    alias = resolve_census("median income", "Ohio", "2021")
    assert alias["get"] == "NAME,B19013_001E"
    assert alias["for"] == "state:39"


def test_census_resolver_rejects_unknown_concept_and_geo():
    assert resolve_census("number_of_unicorns", "Texas", "2022") is None
    assert resolve_census("population", "Atlantis", "2022") is None
    assert resolve_census("population", "Texas", "not-a-year") is None


def test_supported_concepts_nonempty():
    assert "population" in supported_concepts()


# ------------------------------------------------------------------- planning

def test_normalize_year_handles_latest_and_prose():
    assert _normalize_year("2023") == "2023"
    assert _normalize_year("latest") is None
    assert _normalize_year(None) is None
    assert _normalize_year("FY2021 budget") == "2021"


def test_orchestration_dedupes_repeated_queries():
    """A re-plan must never re-issue a query the loop already ran."""
    seen = set()
    tier1 = {"treasury": {"metric": "debt", "year": "2023"}}

    first = _build_tier1_tasks(tier1, seen)
    second = _build_tier1_tasks(tier1, seen)

    assert len(first) == 1
    assert second == []          # deduplicated on the second pass

    # Close the un-awaited coroutines so pytest doesn't warn.
    for _, coro in first:
        coro.close()


def test_bea_plan_expands_year_backoff():
    """BEA should try the requested year plus two fallbacks (data lags publication)."""
    seen = set()
    tier1 = {"bea": {"DataSetName": "NIPA", "TableName": "T31600",
                     "Frequency": "A", "Year": "2023", "LineCode": ["2"]}}
    tasks = _build_tier1_tasks(tier1, seen)
    assert len(tasks) == 3
    years = sorted(k.split("|")[-1] for k, _ in tasks)
    assert years == ["2021", "2022", "2023"]
    for _, coro in tasks:
        coro.close()


def test_invalid_bea_table_is_skipped():
    seen = set()
    tier1 = {"bea": {"TableName": "T99999", "Year": "2023", "LineCode": ["2"]}}
    assert _build_tier1_tasks(tier1, seen) == []


def test_census_concept_query_also_backs_off_across_years():
    """ACS 1-year data lags ~9 months; an unpublished year must not return nothing.

    Regression test: Census previously queried only the exact requested year
    with no fallback, unlike BEA. A claim about the current year (before that
    year's ACS release) silently produced zero evidence.
    """
    seen = set()
    tier1 = {"census": {"concept": "per_capita_income", "geography": "Alaska", "year": "2025"}}
    tasks = _build_tier1_tasks(tier1, seen)

    assert len(tasks) == 3
    years = sorted(k.split("|")[1] for k, _ in tasks)
    assert years == ["2023", "2024", "2025"]
    for _, coro in tasks:
        coro.close()


# --------------------------------------------------- confidence penalty logic

def _svc():
    return VerificationService()


def test_confidence_capped_without_any_datapoint():
    svc = _svc()
    val, notes = svc._apply_confidence_penalties(
        claim="Defense spending exceeded education in 2023.",
        claim_norm="Defense spending exceeded education in 2023.",
        confidence_val=0.88,
        confidence_breakdown={"R": 0.9, "E": 0.8, "S": 0.9},
        sources=[{"url": "https://catalog.data.gov/dataset/a", "snippet": "blurb"}],
        critique={"relevance_score": 0.9},
        verdict="Supported",
    )
    assert val <= 0.40
    assert any("No numeric datapoint" in n for n in notes)


def test_confidence_capped_on_year_mismatch():
    svc = _svc()
    val, notes = svc._apply_confidence_penalties(
        claim="The unemployment rate in 2024 was 3.7%.",
        claim_norm="The unemployment rate in 2024 was 3.7%.",
        confidence_val=0.95,
        confidence_breakdown={"R": 1.0, "E": 1.0, "S": 0.9},
        sources=[{"url": "https://data.bls.gov/x", "data_value": 3.6, "year": "2021"}],
        critique={"relevance_score": 0.9},
        verdict="Supported",
    )
    assert val <= 0.55
    assert any("2024" in n for n in notes)


def test_inconclusive_verdict_never_reads_confident():
    svc = _svc()
    val, _ = svc._apply_confidence_penalties(
        claim="Some claim about 2023.",
        claim_norm="Some claim about 2023.",
        confidence_val=0.9,
        confidence_breakdown={"R": 1.0, "E": 1.0, "S": 1.0},
        sources=[{"url": "https://apps.bea.gov/x", "data_value": 1.0, "year": "2023"}],
        critique={"relevance_score": 0.9},
        verdict="Inconclusive",
    )
    assert val <= 0.45


def test_good_evidence_keeps_high_confidence():
    """Guard against over-penalising: solid, on-year, numeric evidence survives."""
    svc = _svc()
    val, notes = svc._apply_confidence_penalties(
        claim="Federal defense spending exceeded education spending in 2023.",
        claim_norm="Federal defense spending exceeded education spending in 2023.",
        confidence_val=0.88,
        confidence_breakdown={"R": 1.0, "E": 1.0, "S": 0.9},
        sources=[
            {"url": "https://apps.bea.gov/api/data", "data_value": 790895.0, "year": "2023"},
            {"url": "https://apps.bea.gov/api/data", "data_value": 178621.0, "year": "2023"},
        ],
        critique={"relevance_score": 0.95},
        verdict="Supported",
    )
    assert val == 0.88
    assert notes == []


# ------------------------------------------------------------ source ranking

def test_ranking_puts_datapoints_before_catalog_pages():
    svc = _svc()
    catalog = {"url": "https://catalog.data.gov/dataset/a", "snippet": "blurb"}
    datapoint = {"url": "https://apps.bea.gov/api/data", "data_value": 5.0, "year": "2023"}
    ranked = svc._rank_sources([catalog, datapoint], {"relevant_urls": []})
    assert ranked[0] is datapoint
    assert ranked[-1] is catalog


def test_ranking_dedupes_identical_sources():
    svc = _svc()
    a = {"url": "https://apps.bea.gov/x", "data_value": 5.0, "year": "2023", "line_code": "2"}
    b = {"url": "https://apps.bea.gov/x", "data_value": 5.0, "year": "2023", "line_code": "2"}
    ranked = svc._rank_sources([a, b], {"relevant_urls": []})
    assert len(ranked) == 1


def test_plan_has_queries_detection():
    svc = _svc()
    assert svc._plan_has_queries({"tier1_params": {"bea": {"TableName": "T31600"}}}) is True
    assert svc._plan_has_queries({"tier2_keywords": ["gdp 2023"]}) is True
    assert svc._plan_has_queries({"tier1_params": {"bea": None}, "tier2_keywords": []}) is False
    assert svc._plan_has_queries({}) is False


# ----------------------------------------------------------- the loop itself

@pytest.mark.asyncio
async def test_loop_retries_after_empty_first_pass():
    """The core regression: a miss on pass 1 must trigger a re-plan, not a give-up."""
    svc = VerificationService()

    analysis = {
        "claim_normalized": "The Department of Defense budget exceeded $800B in 2023.",
        "claim_type": "quantitative_value",
        "entities": ["Department of Defense", "2023"],
        "relationship": "greater than",
        "api_plan": {"tier1_params": {"bea": None}, "tier2_keywords": ["dod budget"]},
    }

    # Pass 1 returns only a catalog page; pass 2 returns a real datapoint.
    call_count = {"n": 0}

    async def fake_execute(plan, claim_type, seen_keys=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return [{"url": "https://catalog.data.gov/dataset/dod", "snippet": "DoD datasets"}]
        return [{
            "url": "https://api.usaspending.gov/api/v2/references/toptier_agencies/",
            "title": "USAspending: Department of Defense FY2023",
            "snippet": "DoD FY2023 budget authority: $8xx billion",
            "data_value": 8.2e11,
            "year": "2023",
        }]

    critiques = [
        {"sufficient": False, "relevance_score": 0.1, "gap": "no datapoint",
         "relevant_urls": [], "followup_plan": {
             "tier1_params": {"usaspending": {"metric": "agency_budget",
                                              "agency": "Department of Defense",
                                              "year": "2023"}},
             "tier2_keywords": []},
         "gate": {}, "stopped_reason": None},
        {"sufficient": True, "relevance_score": 0.95, "gap": "",
         "relevant_urls": ["https://api.usaspending.gov/api/v2/references/toptier_agencies/"],
         "followup_plan": {}, "gate": {}, "stopped_reason": None},
    ]

    async def fake_critique(**kwargs):
        return critiques[min(kwargs["iteration"] - 1, len(critiques) - 1)]

    async def fake_synthesize(claim, claim_analysis, sources):
        assert any(s.get("data_value") is not None for s in sources), \
            "synthesis should receive the datapoint found on the retry"
        return {"verdict": "Supported", "summary": "DoD budget exceeded $800B.",
                "justification": "USAspending FY2023 shows $820B.", "evidence_links": []}

    with patch("services.verification_service.analyze_claim_for_api_plan",
               AsyncMock(return_value=analysis)), \
         patch("services.verification_service.execute_query_plan", side_effect=fake_execute), \
         patch("services.verification_service.critique_evidence", side_effect=fake_critique), \
         patch("services.verification_service.synthesize_finding_with_llm",
               side_effect=fake_synthesize), \
         patch.object(svc.confidence_scorer, "compute_confidence",
                      AsyncMock(return_value={"confidence": 0.85, "R": 0.95, "E": 0.8, "S": 0.9})):
        result = await svc.verify_claim("The DoD budget was over $800 billion in 2023.")

    assert call_count["n"] == 2, "should have re-planned and queried a second time"
    assert result["verdict"] == "Supported"
    assert len(result["debug_iterations"]) == 2
    assert result["debug_iterations"][0]["sufficient"] is False
    assert result["debug_iterations"][1]["sufficient"] is True


@pytest.mark.asyncio
async def test_loop_stops_when_no_followup_plan_available():
    """Without a usable follow-up plan the loop concludes rather than spinning."""
    svc = VerificationService()
    analysis = {
        "claim_normalized": "x", "claim_type": "other", "entities": [], "relationship": "u",
        "api_plan": {"tier1_params": {}, "tier2_keywords": ["x"]},
    }
    calls = {"n": 0}

    async def fake_execute(plan, claim_type, seen_keys=None):
        calls["n"] += 1
        return []

    async def fake_critique(**kwargs):
        return {"sufficient": False, "relevance_score": 0.0, "gap": "nothing",
                "relevant_urls": [], "followup_plan": {}, "gate": {}, "stopped_reason": None}

    with patch("services.verification_service.analyze_claim_for_api_plan",
               AsyncMock(return_value=analysis)), \
         patch("services.verification_service.execute_query_plan", side_effect=fake_execute), \
         patch("services.verification_service.critique_evidence", side_effect=fake_critique), \
         patch("services.verification_service.synthesize_finding_with_llm",
               AsyncMock(return_value={"verdict": "Inconclusive", "summary": "No data.",
                                       "justification": "", "evidence_links": []})), \
         patch.object(svc.confidence_scorer, "compute_confidence",
                      AsyncMock(return_value={"confidence": 0.3, "R": 0.5, "E": 0.0, "S": 0.2})):
        result = await svc.verify_claim("some claim")

    assert calls["n"] == 1, "should not retry without a follow-up plan"
    assert result["verdict"] == "Inconclusive"
    assert result["confidence"] <= 0.45


@pytest.mark.asyncio
async def test_loop_respects_max_iterations():
    """A persistently-failing claim must terminate at the configured budget."""
    from config.constants import AGENT_CONFIG
    svc = VerificationService()
    analysis = {
        "claim_normalized": "x", "claim_type": "other", "entities": [], "relationship": "u",
        "api_plan": {"tier1_params": {}, "tier2_keywords": ["x"]},
    }
    calls = {"n": 0}

    async def fake_execute(plan, claim_type, seen_keys=None):
        calls["n"] += 1
        return [{"url": "https://catalog.data.gov/dataset/x", "snippet": "blurb"}]

    async def fake_critique(**kwargs):
        # Never satisfied, always offers another plan -> only the cap stops it.
        return {"sufficient": False, "relevance_score": 0.1, "gap": "still nothing",
                "relevant_urls": [], "followup_plan": {"tier2_keywords": ["another try"]},
                "gate": {}, "stopped_reason": None}

    with patch("services.verification_service.analyze_claim_for_api_plan",
               AsyncMock(return_value=analysis)), \
         patch("services.verification_service.execute_query_plan", side_effect=fake_execute), \
         patch("services.verification_service.critique_evidence", side_effect=fake_critique), \
         patch("services.verification_service.synthesize_finding_with_llm",
               AsyncMock(return_value={"verdict": "Inconclusive", "summary": "No data.",
                                       "justification": "", "evidence_links": []})), \
         patch.object(svc.confidence_scorer, "compute_confidence",
                      AsyncMock(return_value={"confidence": 0.3, "R": 0.7, "E": 0.2, "S": 0.2})):
        result = await svc.verify_claim("some claim")

    assert calls["n"] == AGENT_CONFIG.MAX_ITERATIONS
    assert len(result["debug_iterations"]) == AGENT_CONFIG.MAX_ITERATIONS
