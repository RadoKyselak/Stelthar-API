import asyncio
import os
import re
from typing import Dict, Any, List
from config import BEA_VALID_TABLES, LIVE_WEB_SEARCH_URL, logger
from api import query_bea, query_census_acs, query_bls, query_congress, query_datagov, query_live_web

async def execute_query_plan(plan: Dict[str, Any], claim_type: str) -> List[Dict[str, Any]]:
    """
    Execute an API query plan by calling appropriate APIs.
    Args:
        plan: The API plan from analyze_claim_for_api_plan
        claim_type: Type of claim being verified
    Returns:
        List of results from all API calls
    """
    tier1 = plan.get("tier1_params", {}) or {}
    tier2_kws = plan.get("tier2_keywords", []) or []

    tasks = []

    if bea_params := tier1.get("bea"):
        if isinstance(bea_params, dict):
            table = bea_params.get("TableName")
            if table and table in BEA_VALID_TABLES:
                line_codes = bea_params.get("LineCode")
                codes_to_run = []
                if isinstance(line_codes, list):
                    codes_to_run = [str(c).strip() for c in line_codes if str(c).strip()]
                elif line_codes:
                    codes_to_run = [str(line_codes).strip()]

                for code in codes_to_run:
                    if re.match(r"^[A-Z]?\d+[A-Z]?\d*$", code) or code.isdigit():
                        params_copy = bea_params.copy()
                        params_copy["LineCode"] = code
                        tasks.append(query_bea(params_copy))

                        year_str = str(params_copy.get("Year", "")).strip()
                        if year_str.isdigit():
                            y = int(year_str)
                            for backoff in (1, 2):
                                fallback = params_copy.copy()
                                fallback["Year"] = str(y - backoff)
                                tasks.append(query_bea(fallback))
                    else:
                        logger.warning("Skipping invalid BEA LineCode format in plan: %s", code)
            elif table:
                logger.warning("BEA table specified in plan but not in supported list: %s", table)
        else:
            logger.warning("BEA parameters in plan are not a dictionary: %s", bea_params)

    if census_params := tier1.get("census_acs"):
        if isinstance(census_params, dict) and all(k in census_params for k in ["year", "dataset", "get", "for"]):
            tasks.append(asyncio.create_task(query_census_acs(params=census_params)))
        elif isinstance(census_params, dict):
            logger.warning("Census ACS plan missing required parameters: %s", census_params)

    if bls_params := tier1.get("bls"):
        if isinstance(bls_params, dict) and all(k in bls_params for k in ["metric", "year"]):
            tasks.append(asyncio.create_task(query_bls(params=bls_params)))
        elif isinstance(bls_params, dict):
            logger.warning("BLS plan missing required parameters: %s", bls_params)

    unique_kws = sorted(list(set(kw for kw in tier2_kws if isinstance(kw, str) and kw.strip())))

    for kw in unique_kws:
        tasks.append(query_datagov(kw))
        tasks.append(query_live_web(kw, base_url=LIVE_WEB_SEARCH_URL))
        if claim_type == "legislative" or any(token in kw.lower() for token in [" bill", "act", " law", "h.r.", "s."]):
            tasks.append(query_congress(keyword_query=kw))

    if not tasks:
        logger.warning("No API calls generated for the plan.")
        return []

    done, pending = await asyncio.wait(tasks, timeout=ORCHESTRATION_TIMEOUT_SECONDS)

    results = []
    for task in done:
        try:
            results.append(task.result())
        except Exception as e:
            results.append(e)

    for task in pending:
        task.cancel()

    processed_results = []
    if pending:
        logger.warning("Orchestration timeout reached (%.1fs). Completed %d/%d tasks.", ORCHESTRATION_TIMEOUT_SECONDS, len(done), len(tasks))
        processed_results.append({
            "error": f"Orchestration timeout after {ORCHESTRATION_TIMEOUT_SECONDS:.1f}s",
            "source": "internal",
            "status": "failed"
        })

    for i, res in enumerate(results):
        if isinstance(res, Exception):
            logger.error(f"Error during API call task index {i}: {res}", exc_info=True)
            processed_results.append({
                "error": f"Task execution failed: {type(res).__name__}",
                "source": "internal",
                "status": "failed"
            })
        elif isinstance(res, list):
            processed_results.extend(res)
        elif isinstance(res, dict) and "error" in res:
            processed_results.append(res)
        elif res is not None:
            logger.warning(f"Unexpected result type from task index {i}: {type(res)}. Result: {res}")

    return processed_results
