"""Evidence critic: the component that stops the pipeline from giving up.

The original flow was single-shot — plan once, query once, synthesize once. When
results came back empty or irrelevant it returned "Inconclusive" and ended. This
module inspects what actually came back, decides whether it genuinely answers the
claim, and when it doesn't, proposes a corrected follow-up query plan so the
verification loop can try again.

Two layers, deliberately:
  1. A deterministic gate (no LLM) that catches the obvious failure mode —
     zero data-bearing sources, or only catalog/landing-page metadata.
  2. An LLM critique that judges topical relevance and names the specific gap.
"""
from typing import Dict, Any, List, Optional

from config import logger
from utils.parsing import extract_json_block
from .llm import call_gemini

# Sources from these hosts are catalog/landing pages: they describe that a
# dataset exists, they do not contain the value needed to settle a claim.
_METADATA_ONLY_HINTS = ("catalog.data.gov", "/dataset/")


def is_data_bearing(source: Dict[str, Any]) -> bool:
    """True if the source carries an actual numeric datapoint."""
    return isinstance(source, dict) and source.get("data_value") is not None


def is_metadata_only(source: Dict[str, Any]) -> bool:
    """True if the source is a dataset catalog entry rather than real data."""
    if not isinstance(source, dict) or is_data_bearing(source):
        return False
    url = (source.get("url") or "").lower()
    return any(hint in url for hint in _METADATA_ONLY_HINTS)


def deterministic_gate(sources: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Cheap, LLM-free assessment of whether evidence is worth synthesizing."""
    valid = [s for s in sources if isinstance(s, dict) and "error" not in s]
    data_bearing = [s for s in valid if is_data_bearing(s)]
    metadata_only = [s for s in valid if is_metadata_only(s)]

    return {
        "total_sources": len(valid),
        "data_bearing_count": len(data_bearing),
        "metadata_only_count": len(metadata_only),
        # No numbers at all means we cannot settle a quantitative claim.
        "has_hard_evidence": len(data_bearing) > 0,
        # Everything we got was a catalog blurb -> the classic stale/irrelevant case.
        "only_metadata": len(valid) > 0 and len(data_bearing) == 0 and len(metadata_only) == len(valid),
    }


def _summarize_sources_for_critic(sources: List[Dict[str, Any]], limit: int = 12) -> str:
    lines = []
    for idx, s in enumerate(sources[:limit]):
        url = s.get("url", "N/A")
        title = s.get("title", "N/A")
        snippet = (s.get("snippet") or "")[:220]
        value = s.get("data_value")
        year = s.get("year") or s.get("raw_year") or ""
        lines.append(
            f"[{idx + 1}] title={title!r} year={year!r} "
            f"has_datapoint={'yes' if value is not None else 'no'} "
            f"value={value!r}\n    url={url}\n    snippet={snippet!r}"
        )
    return "\n".join(lines) if lines else "(no sources retrieved)"


async def critique_evidence(
    claim: str,
    analysis: Dict[str, Any],
    sources: List[Dict[str, Any]],
    iteration: int,
    max_iterations: int,
) -> Dict[str, Any]:
    """Judge evidence sufficiency and propose a follow-up plan if lacking.

    Returns a dict with:
        sufficient (bool), relevance_score (float 0-1), gap (str),
        relevant_urls (List[str]), followup_plan (api_plan-shaped dict)
    """
    gate = deterministic_gate(sources)
    valid = [s for s in sources if isinstance(s, dict) and "error" not in s]

    # Last iteration: no point critiquing, we must synthesize with what we have.
    if iteration >= max_iterations:
        return {
            "sufficient": True,
            "relevance_score": 1.0 if gate["has_hard_evidence"] else 0.2,
            "gap": "" if gate["has_hard_evidence"] else "No hard datapoint found within the iteration budget.",
            "relevant_urls": [s.get("url") for s in valid if s.get("url")],
            "followup_plan": {},
            "gate": gate,
            "stopped_reason": "max_iterations",
        }

    # Fast path: nothing at all, or only catalog metadata -> definitely retry.
    # Skip the LLM call; we already know it's insufficient.
    if not valid or gate["only_metadata"]:
        reason = (
            "No sources were retrieved." if not valid
            else "Only dataset catalog pages were retrieved; none contain an actual value."
        )
        logger.info("Critic (deterministic): insufficient — %s", reason)
        followup = await _propose_followup(claim, analysis, valid, reason)
        return {
            "sufficient": False,
            "relevance_score": 0.0,
            "gap": reason,
            "relevant_urls": [],
            "followup_plan": followup,
            "gate": gate,
            "stopped_reason": None,
        }

    prompt = f"""
You are the evidence critic in a fact-checking pipeline that verifies claims against
official U.S. government data. Your job is NOT to decide if the claim is true. Your job
is to decide whether the retrieved evidence is good enough to decide it.

CLAIM: '''{claim}'''
Normalized: {analysis.get('claim_normalized', claim)}
Claim type: {analysis.get('claim_type', 'unknown')}
Entities: {analysis.get('entities', [])}
Asserted relationship: {analysis.get('relationship', 'unknown')}

RETRIEVED EVIDENCE:
{_summarize_sources_for_critic(valid)}

Judge strictly:
- Evidence is SUFFICIENT only if it contains an actual datapoint (a number, or a
  definitive factual statement) that directly addresses the claim's entities AND
  timeframe. A dataset description, catalog page, or a source that merely mentions
  the topic is NOT sufficient.
- If the claim names a year, evidence from a different year is NOT sufficient on its
  own — note the mismatch in "gap".
- Be honest: it is far better to request another query than to settle for loosely
  related data.

Return ONLY a JSON object:
{{
  "sufficient": true|false,
  "relevance_score": 0.0-1.0,
  "relevant_source_numbers": [1, 3],
  "gap": "what specifically is missing, empty string if sufficient"
}}
"""

    try:
        res = await call_gemini(prompt)
        parsed = extract_json_block(res.get("text", "")) or {}
    except Exception as e:
        logger.warning("Critic LLM call failed (%s); falling back to deterministic gate.", e)
        parsed = {}

    if not parsed:
        # Fall back to the deterministic signal rather than looping blindly.
        sufficient = gate["has_hard_evidence"]
        return {
            "sufficient": sufficient,
            "relevance_score": 0.6 if sufficient else 0.2,
            "gap": "" if sufficient else "No datapoint found in retrieved sources.",
            "relevant_urls": [s.get("url") for s in valid if s.get("url")],
            "followup_plan": {} if sufficient else await _propose_followup(
                claim, analysis, valid, "No datapoint found in retrieved sources."
            ),
            "gate": gate,
            "stopped_reason": None,
        }

    sufficient = bool(parsed.get("sufficient"))
    try:
        relevance = float(parsed.get("relevance_score", 0.0))
    except (TypeError, ValueError):
        relevance = 0.0
    relevance = max(0.0, min(1.0, relevance))
    gap = str(parsed.get("gap", "") or "")

    # Map 1-indexed source numbers back to URLs.
    relevant_urls: List[str] = []
    nums = parsed.get("relevant_source_numbers")
    if isinstance(nums, list):
        for n in nums:
            try:
                i = int(n) - 1
            except (TypeError, ValueError):
                continue
            if 0 <= i < len(valid) and valid[i].get("url"):
                relevant_urls.append(valid[i]["url"])

    followup = {}
    if not sufficient:
        followup = await _propose_followup(claim, analysis, valid, gap)

    logger.info(
        "Critic: sufficient=%s relevance=%.2f gap=%r", sufficient, relevance, gap[:120]
    )

    return {
        "sufficient": sufficient,
        "relevance_score": relevance,
        "gap": gap,
        "relevant_urls": relevant_urls,
        "followup_plan": followup,
        "gate": gate,
        "stopped_reason": None,
    }


async def _propose_followup(
    claim: str,
    analysis: Dict[str, Any],
    sources: List[Dict[str, Any]],
    gap: str,
) -> Dict[str, Any]:
    """Ask the planner LLM for a corrected query plan targeting the identified gap."""
    tried = analysis.get("api_plan", {})
    prompt = f"""
A fact-checking query plan failed to retrieve usable evidence. Design a DIFFERENT plan
that targets the gap. Do not repeat the previous plan.

CLAIM: '''{claim}'''
Claim type: {analysis.get('claim_type', 'unknown')}
Entities: {analysis.get('entities', [])}

PREVIOUS PLAN (did not work):
{tried}

WHAT WAS MISSING: {gap or 'No usable datapoint was retrieved.'}

AVAILABLE DATA SOURCES (all official, keyless unless noted):
- "usaspending": federal AGENCY budgets & spending by fiscal year. Best for
  "Department/Agency X spent/was budgeted $Y in FY Z".
  Params: {{"metric": "agency_budget"|"total_spending", "agency": "<full agency name>", "year": "<YYYY>"}}
- "treasury": U.S. national debt. Params: {{"metric": "debt", "year": "<YYYY>"}} (omit year for latest)
- "bea": macroeconomic aggregates (GDP, personal income, government receipts &
  expenditures by function, trade). Params: {{"DataSetName":"NIPA","TableName":"<T#####>","Frequency":"A","Year":"<YYYY>","LineCode":["<n>"]}}
- "bls": labor & prices. Params: {{"metric": "unemployment"|"cpi"|"cpi_core"|"ppi"|"labor_force_participation"|"employment_level"|"avg_hourly_earnings", "year": "<YYYY>"}}
- "census": demographics. Use FRIENDLY form: {{"concept": "population"|"median_household_income"|"poverty_rate"|"median_age"|"median_home_value"|"unemployment_rate"|"bachelors_or_higher"|"uninsured_rate"|"per_capita_income"|"households", "geography": "<state name or US>", "year": "<YYYY>"}}
- "tier2_keywords": research search across official U.S. government sites (search.gov)
  and Congress.gov for bills. Fetches real page/report content, not just a snippet —
  use this for qualitative or research questions (agency statements, GAO/CBO reports,
  narrative context) that the structured sources above cannot answer, or when a claim
  has no clean quantitative shape at all.

Strategy hints:
- If the claim is a specific number/statistic, prefer the structured sources above —
  they return exact values with no extraction risk.
- If the previous plan used BEA and got nothing, try "usaspending" for agency-level
  spending, or a different BEA TableName/LineCode.
- If a specific year returned nothing, the data may not be published yet — try the
  prior year and say so.
- If the claim is qualitative/research-oriented (no single number to check), rely on
  tier2_keywords — it now returns real fetched content, not just dataset descriptions.

Return ONLY a JSON object shaped like:
{{"tier1_params": {{"bea": null, "census": null, "bls": null, "usaspending": null, "treasury": null}}, "tier2_keywords": []}}
"""
    try:
        res = await call_gemini(prompt)
        parsed = extract_json_block(res.get("text", ""))
        if not isinstance(parsed, dict):
            return {}
        parsed.setdefault("tier1_params", {})
        kws = parsed.get("tier2_keywords")
        parsed["tier2_keywords"] = [k for k in kws if isinstance(k, str) and k.strip()] if isinstance(kws, list) else []
        return parsed
    except Exception as e:
        logger.warning("Follow-up plan generation failed: %s", e)
        return {}
