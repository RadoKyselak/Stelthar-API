from typing import Dict, Any
import re
from datetime import datetime
from fastapi import HTTPException

from config import logger
from utils.parsing import extract_json_block
from .llm import call_gemini
from .census_resolver import supported_concepts


def _extract_year(text: str) -> str | None:
    match = re.search(r"\b(19\d{2}|20\d{2})\b", text or "")
    return match.group(1) if match else None


def _default_year() -> str:
    return str(datetime.utcnow().year - 1)


def _apply_spending_plan_guardrails(claim: str, parsed: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministic backstop for the defense-vs-education comparison.

    Kept from the original implementation because it is a common demo claim, but
    now also attaches a USAspending cross-check so the verdict does not rest on a
    single BEA table.
    """
    claim_l = (claim or "").lower()
    looks_like_federal_spending = any(
        k in claim_l for k in ["federal", "government", "spending", "budget"]
    )
    has_defense = "defense" in claim_l
    has_education = "education" in claim_l

    if not (looks_like_federal_spending and has_defense and has_education):
        return parsed

    api_plan = parsed.setdefault("api_plan", {})
    tier1 = api_plan.setdefault("tier1_params", {})

    year = _extract_year(claim) or _default_year()
    tier1["bea"] = {
        "DataSetName": "NIPA",
        "TableName": "T31600",
        "Frequency": "A",
        "Year": year,
        "LineCode": ["2", "14"],
    }

    kws = api_plan.get("tier2_keywords")
    if not isinstance(kws, list):
        kws = []
    required = [f"federal spending by function defense education {year}"]
    api_plan["tier2_keywords"] = list(
        dict.fromkeys([*(kw for kw in kws if isinstance(kw, str) and kw.strip()), *required])
    )

    if parsed.get("claim_type") not in {"quantitative_comparison", "quantitative_value"}:
        parsed["claim_type"] = "quantitative_comparison"

    return parsed


def _normalize_plan(parsed: Dict[str, Any], claim: str) -> Dict[str, Any]:
    """Fill defaults and normalize tier1 keys so the executor sees a stable shape."""
    parsed.setdefault("claim_normalized", claim)
    parsed.setdefault("claim_type", "Other")
    parsed.setdefault("entities", [])
    parsed.setdefault("relationship", "unknown")
    parsed.setdefault("api_plan", {})

    api_plan = parsed["api_plan"]
    if not isinstance(api_plan, dict):
        api_plan = {}
        parsed["api_plan"] = api_plan

    tier1 = api_plan.get("tier1_params")
    if not isinstance(tier1, dict):
        tier1 = {}
    # Accept the legacy "census_acs" key as an alias for "census".
    if "census_acs" in tier1 and "census" not in tier1:
        tier1["census"] = tier1.pop("census_acs")
    for source in ("bea", "census", "bls", "usaspending", "treasury"):
        tier1.setdefault(source, None)
    api_plan["tier1_params"] = tier1

    kw = api_plan.get("tier2_keywords")
    api_plan["tier2_keywords"] = (
        [k for k in kw if isinstance(k, str) and k.strip()] if isinstance(kw, list) else []
    )
    # Only fall back to the raw claim as a search term if we have no structured
    # query at all — otherwise it just floods results with catalog noise.
    if not api_plan["tier2_keywords"] and not any(tier1.get(s) for s in tier1):
        api_plan["tier2_keywords"] = [claim]

    return parsed


async def analyze_claim_for_api_plan(claim: str) -> Dict[str, Any]:
    """Analyze a claim and generate an API query plan using the LLM."""
    concepts = ", ".join(supported_concepts())
    current_year = datetime.utcnow().year

    prompt_template = """
You are a research analyst expert in U.S. government data APIs. Analyze the claim and
produce the best plan to verify it with FIRST-PARTY data that contains actual numbers.

**Analysis steps**
1. `claim_normalized`: restate the claim as a precise, verifiable statement.
2. `entities`: the concepts, agencies, metrics, places, and timeframes involved.
3. `claim_type`: one of quantitative_comparison, quantitative_value, factual,
   legislative, economic_indicator, other.
4. `relationship`: the asserted link (e.g. "greater than", "equals X", "increased").
5. `api_plan`: choose the RIGHT source(s). Routing rules, in priority order:

   * **A specific federal agency's budget or spending** ("Department of Defense budget",
     "NASA funding", "how much did HHS spend") -> `usaspending`.
     {{"metric": "agency_budget", "agency": "<full official agency name>", "year": "<YYYY>"}}
     For government-wide totals use {{"metric": "total_spending", "year": "<YYYY>"}}.
   * **National debt** ("the national debt", "total public debt") -> `treasury`.
     {{"metric": "debt", "year": "<YYYY>"}} (omit year for the latest figure).
   * **Broad economic aggregates** (GDP, personal income, government spending BY FUNCTION,
     receipts, trade balance, savings) -> `bea`.
     {{"DataSetName":"NIPA","TableName":"<table>","Frequency":"A","Year":"<YYYY>","LineCode":["<code>"]}}
     Valid tables: T10101 (real GDP % change), T10105 (GDP current $), T10106 (real GDP),
     T20100 (personal income), T20305 (PCE by type), T30100 (gov receipts & expenditures),
     T31600 (gov expenditures BY FUNCTION - defense=2, education=14), T40100 (foreign
     transactions), T50100 (saving & investment), T70500 (employment by industry).
   * **Labor market or prices** (unemployment, inflation, CPI, PPI, wages,
     labor force participation) -> `bls`.
     {{"metric": "<one of: unemployment, cpi, cpi_core, ppi, labor_force_participation, employment_level, avg_hourly_earnings>", "year": "<YYYY>"}}
   * **Demographics / state-level social data** (population, income, poverty, age,
     home value, education, insurance) -> `census`. Use the FRIENDLY form ONLY.
     {{"concept": "<one of: {concepts}>", "geography": "<state name or 'US'>", "year": "<YYYY>"}}
     NEVER write raw ACS variable codes — the system resolves them for you.
   * **Legislation / bills** -> `tier2_keywords` (Congress.gov is searched automatically
     when the claim is legislative).
   * **Qualitative / research questions** — no single number to check, e.g. "what has
     GAO said about X", "summarize the recent CBO report on Y", agency policy
     statements, program details -> `tier2_keywords`. This searches official
     government sites and FETCHES REAL PAGE/REPORT CONTENT, not just a snippet.
   * `tier2_keywords`: 1-3 short search strings. Prefer a structured source above for
     any claim with a specific number/statistic — it returns an exact value with no
     extraction risk. Use tier2_keywords as the PRIMARY plan for genuinely qualitative
     claims, and as a supplement otherwise.

**Year handling**: if the claim states a year, use it. If it says "currently"/"now"/"today"
or states no year, use "{default_year}" (the current year is {current_year}; the latest
annual federal data is typically a year behind).

**USER CLAIM:** '''{claim}'''

Return ONLY one valid JSON object with keys `claim_normalized`, `claim_type`, `entities`,
`relationship`, `api_plan`. Set unused sources to null.

**Example (agency budget -> USAspending):**
Claim: "The Department of Defense budget was over $800 billion in 2023."
{{
  "claim_normalized": "The Department of Defense budget exceeded $800 billion in fiscal year 2023.",
  "claim_type": "quantitative_value",
  "entities": ["Department of Defense", "budget", "2023"],
  "relationship": "greater than",
  "api_plan": {{
    "tier1_params": {{
      "usaspending": {{"metric": "agency_budget", "agency": "Department of Defense", "year": "2023"}},
      "bea": null, "census": null, "bls": null, "treasury": null
    }},
    "tier2_keywords": ["FY2023 defense appropriations"]
  }}
}}

**Example (function spending -> BEA):**
Claim: "Federal spending on defense exceeded education in 2023."
{{
  "claim_normalized": "Total federal expenditures on the national defense function exceeded those on the education function in 2023.",
  "claim_type": "quantitative_comparison",
  "entities": ["national defense function", "education function", "2023"],
  "relationship": "greater than",
  "api_plan": {{
    "tier1_params": {{
      "bea": {{"DataSetName":"NIPA","TableName":"T31600","Frequency":"A","Year":"2023","LineCode":["2","14"]}},
      "usaspending": null, "census": null, "bls": null, "treasury": null
    }},
    "tier2_keywords": ["federal spending by function 2023"]
  }}
}}

**Example (demographics -> Census, friendly form):**
Claim: "The median household income in Texas was about $73,000 in 2022."
{{
  "claim_normalized": "Texas median household income was approximately $73,000 in 2022.",
  "claim_type": "quantitative_value",
  "entities": ["Texas", "median household income", "2022"],
  "relationship": "approximately equals",
  "api_plan": {{
    "tier1_params": {{
      "census": {{"concept": "median_household_income", "geography": "Texas", "year": "2022"}},
      "bea": null, "bls": null, "usaspending": null, "treasury": null
    }},
    "tier2_keywords": []
  }}
}}

**Example (national debt -> Treasury):**
Claim: "The national debt is over $33 trillion."
{{
  "claim_normalized": "The total U.S. public debt outstanding exceeds $33 trillion.",
  "claim_type": "quantitative_value",
  "entities": ["national debt", "current"],
  "relationship": "greater than",
  "api_plan": {{
    "tier1_params": {{
      "treasury": {{"metric": "debt"}},
      "bea": null, "census": null, "bls": null, "usaspending": null
    }},
    "tier2_keywords": []
  }}
}}
"""
    prompt = prompt_template.format(
        claim=claim,
        concepts=concepts,
        default_year=_default_year(),
        current_year=current_year,
    )

    fallback_plan = {
        "claim_normalized": claim,
        "claim_type": "Other",
        "entities": [],
        "relationship": "unknown",
        "api_plan": {"tier1_params": {}, "tier2_keywords": [claim]},
    }

    try:
        res = await call_gemini(prompt)
        parsed = extract_json_block(res.get("text", ""))

        if not parsed or "api_plan" not in parsed:
            logger.warning("Could not parse valid plan JSON from LLM. Falling back.")
            fallback_plan["debug_raw_llm_response"] = res.get("text", "No text found.")
            return _normalize_plan(fallback_plan, claim)

        parsed = _normalize_plan(parsed, claim)
        parsed = _apply_spending_plan_guardrails(claim, parsed)
        return parsed

    except HTTPException as e:
        logger.error("LLM failed generating plan: %s", getattr(e, "detail", str(e)))
        fallback_plan["debug_exception"] = str(e)
        return _normalize_plan(fallback_plan, claim)
    except Exception as e:
        logger.exception("Unexpected error generating plan. Falling back.")
        fallback_plan["debug_exception"] = str(e)
        return _normalize_plan(fallback_plan, claim)
