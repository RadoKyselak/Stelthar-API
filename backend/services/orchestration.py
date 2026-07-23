"""Query-plan execution across all government data sources.

Two tiers:
  * Tier 1 — structured sources (BEA, Census, BLS, USAspending, Treasury) that
    return an exact, free, zero-hallucination-risk number. Always tried first.
  * Tier 2 — the Tavily research harness, for anything structured sources
    can't answer: qualitative claims, agency reports, GAO/CBO analysis. This
    replaced Data.gov catalog search as the default fallback, because catalog
    pages are dataset descriptions, not evidence — they were the direct cause
    of "surfaces old irrelevant data." query_datagov is kept as a module for
    discovery use cases but is no longer wired into the default fan-out.

Also: deduplicates queries across loop iterations so a re-plan never re-runs
work, and fixes the previously-undefined ORCHESTRATION_TIMEOUT_SECONDS that
made this module raise NameError on every request that produced tasks.
"""
import asyncio
import re
from datetime import datetime
from typing import Dict, Any, List, Optional, Set, Tuple

from config import BEA_VALID_TABLES, logger
from config.constants import AGENT_CONFIG, BLS_SERIES_CATALOG
from api import (
    query_bea,
    query_census_acs,
    query_bls,
    query_congress,
    query_tavily,
    query_usaspending,
    query_treasury,
)
from .census_resolver import resolve_census


def _default_year() -> str:
    """Most recent year with a realistic chance of published annual data."""
    return str(datetime.utcnow().year - 1)


def _normalize_year(value: Any) -> Optional[str]:
    """Accept 'latest'/None/int/str; return a concrete 4-digit year or None."""
    if value is None:
        return None
    s = str(value).strip().lower()
    if not s or s in ("latest", "most recent", "current", "recent"):
        return None
    m = re.search(r"(19\d{2}|20\d{2})", s)
    return m.group(1) if m else None


def _key(*parts: Any) -> str:
    return "|".join(str(p) for p in parts)


def _build_tier1_tasks(
    tier1: Dict[str, Any],
    seen: Set[str],
) -> List[Tuple[str, Any]]:
    """Return (dedup_key, coroutine) pairs for structured API queries."""
    tasks: List[Tuple[str, Any]] = []

    # ---- BEA -------------------------------------------------------------
    bea_params = tier1.get("bea")
    if isinstance(bea_params, dict) and bea_params:
        table = bea_params.get("TableName")
        if table and table in BEA_VALID_TABLES:
            line_codes = bea_params.get("LineCode")
            if isinstance(line_codes, list):
                codes = [str(c).strip() for c in line_codes if str(c).strip()]
            elif line_codes:
                codes = [str(line_codes).strip()]
            else:
                codes = []

            year = _normalize_year(bea_params.get("Year")) or _default_year()

            for code in codes:
                if not (re.match(r"^[A-Z]?\d+[A-Z]?\d*$", code) or code.isdigit()):
                    logger.warning("Skipping invalid BEA LineCode format: %s", code)
                    continue
                # Query the requested year, then step back two years. Federal
                # annual data is often not yet published for the latest year;
                # without this the pipeline returns nothing at all.
                for backoff in (0, 1, 2):
                    p = dict(bea_params)
                    p["LineCode"] = code
                    p["Year"] = str(int(year) - backoff)
                    k = _key("bea", table, code, p["Year"])
                    if k in seen:
                        continue
                    seen.add(k)
                    tasks.append((k, query_bea(p)))
        elif table:
            logger.warning("BEA table not in supported list: %s", table)

    # ---- Census (friendly concept OR raw params) --------------------------
    census_params = tier1.get("census") or tier1.get("census_acs")
    if isinstance(census_params, dict) and census_params:
        if census_params.get("concept"):
            year = _normalize_year(census_params.get("year")) or _default_year()
            concept = census_params.get("concept", "")
            geography = census_params.get("geography", "")

            # ACS 1-year estimates publish ~9 months after year-end, so the
            # requested year (especially "current"/default year) is often not
            # out yet. Without this, a query for an unpublished year silently
            # returned nothing — the same failure mode BEA already guards
            # against. Try the requested year, then step back up to 2 years.
            for backoff in (0, 1, 2):
                y = str(int(year) - backoff) if year.isdigit() else year
                resolved = resolve_census(concept, geography, y)
                if not resolved:
                    if backoff == 0:
                        logger.warning(
                            "Census concept could not be resolved: concept=%r geography=%r",
                            concept, geography,
                        )
                    continue
                k = _key("census", resolved.get("year"), resolved.get("dataset"),
                         resolved.get("get"), resolved.get("for"))
                if k not in seen:
                    seen.add(k)
                    tasks.append((k, query_census_acs(params=resolved)))
        elif all(k in census_params and census_params[k] for k in ("year", "dataset", "get", "for")):
            resolved = dict(census_params)
            k = _key("census", resolved.get("year"), resolved.get("dataset"),
                     resolved.get("get"), resolved.get("for"))
            if k not in seen:
                seen.add(k)
                tasks.append((k, query_census_acs(params=resolved)))

    # ---- BLS -------------------------------------------------------------
    bls_params = tier1.get("bls")
    if isinstance(bls_params, dict) and bls_params:
        metric = str(bls_params.get("metric", "")).strip().lower()
        if metric in BLS_SERIES_CATALOG:
            year = _normalize_year(bls_params.get("year")) or _default_year()
            k = _key("bls", metric, year)
            if k not in seen:
                seen.add(k)
                tasks.append((k, query_bls({"metric": metric, "year": year})))
        elif metric:
            logger.warning("BLS metric not in catalog: %s", metric)

    # ---- USAspending (keyless) -------------------------------------------
    usa_params = tier1.get("usaspending")
    if isinstance(usa_params, dict) and usa_params:
        year = _normalize_year(usa_params.get("year")) or _default_year()
        metric = str(usa_params.get("metric", "agency_budget")).strip().lower()
        agency = str(usa_params.get("agency", "")).strip()
        k = _key("usaspending", metric, agency.lower(), year)
        if k not in seen:
            seen.add(k)
            tasks.append((k, query_usaspending({"metric": metric, "agency": agency, "year": year})))

    # ---- Treasury (keyless) ----------------------------------------------
    treasury_params = tier1.get("treasury")
    if isinstance(treasury_params, dict) and treasury_params:
        metric = str(treasury_params.get("metric", "debt")).strip().lower()
        year = _normalize_year(treasury_params.get("year")) or ""
        k = _key("treasury", metric, year or "latest")
        if k not in seen:
            seen.add(k)
            tasks.append((k, query_treasury({"metric": metric, "year": year})))

    return tasks


# Matches actual bill designators (S. 1234, H.R. 1234, H.Res. 12, S.Res. 12 —
# always letters, a period, and a bill number). The previous check used a bare
# substring test including "s.", which matches "U.S." — so any claim mentioning
# "U.S." (nearly all of them) falsely triggered a Congress.gov bill search,
# polluting evidence with irrelevant bills matched on unrelated keywords.
_BILL_DESIGNATOR_RE = re.compile(r"\b(h\.\s?r\.|h\.\s?res\.|s\.\s?res\.|s\.)\s?\d+\b", re.IGNORECASE)


def _looks_legislative(kw: str) -> bool:
    kw_lower = kw.lower()
    if any(t in kw_lower for t in (" bill", " act", " law")):
        return True
    return bool(_BILL_DESIGNATOR_RE.search(kw))


def _build_tier2_tasks(
    keywords: List[str],
    claim_type: str,
    seen: Set[str],
) -> List[Tuple[str, Any]]:
    """Research queries: the Tavily harness, plus Congress.gov for bills.

    Unlike tier1, these return LLM-extracted excerpts rather than exact
    structured values — used when tier1 can't answer the claim at all, or as
    supplementary context (agency statements, GAO/CBO reports) even when it can.
    """
    tasks: List[Tuple[str, Any]] = []
    unique = sorted({kw.strip() for kw in keywords if isinstance(kw, str) and kw.strip()})

    # Cap breadth: fanning out every keyword to multiple APIs with no limit
    # floods the context with low-value hits and multiplies fetch cost.
    for kw in unique[:4]:
        k = _key("tavily", kw)
        if k not in seen:
            seen.add(k)
            tasks.append((k, query_tavily(kw)))

        if claim_type == "legislative" or _looks_legislative(kw):
            k = _key("congress", kw)
            if k not in seen:
                seen.add(k)
                tasks.append((k, query_congress(keyword_query=kw)))

    return tasks


async def execute_query_plan(
    plan: Dict[str, Any],
    claim_type: str,
    seen_keys: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    """Execute an API query plan.

    Args:
        plan: api_plan with tier1_params / tier2_keywords
        claim_type: classification of the claim
        seen_keys: mutable set of already-issued query keys; pass the same set
            across loop iterations so re-planning never repeats a query.
    """
    seen = seen_keys if seen_keys is not None else set()
    tier1 = plan.get("tier1_params", {}) or {}
    tier2_kws = plan.get("tier2_keywords", []) or []

    keyed_tasks = _build_tier1_tasks(tier1, seen) + _build_tier2_tasks(tier2_kws, claim_type, seen)

    if not keyed_tasks:
        logger.info("No new API calls generated for this plan (all deduplicated or empty).")
        return []

    logger.info("Dispatching %d source queries.", len(keyed_tasks))

    coros = [c for _, c in keyed_tasks]
    tasks = [asyncio.ensure_future(c) for c in coros]

    done, pending = await asyncio.wait(
        tasks, timeout=AGENT_CONFIG.ITERATION_TIMEOUT_SECONDS
    )

    for task in pending:
        task.cancel()
    if pending:
        # Let cancellations settle so no "task was destroyed" warnings leak.
        await asyncio.gather(*pending, return_exceptions=True)

    processed: List[Dict[str, Any]] = []

    if pending:
        logger.warning(
            "Query timeout (%.1fs). Completed %d/%d.",
            AGENT_CONFIG.ITERATION_TIMEOUT_SECONDS, len(done), len(tasks),
        )
        processed.append({
            "error": f"Source query timeout after {AGENT_CONFIG.ITERATION_TIMEOUT_SECONDS:.1f}s",
            "source": "internal",
            "status": "failed",
        })

    # Preserve deterministic ordering (asyncio.wait returns an unordered set).
    for task in tasks:
        if task not in done:
            continue
        try:
            res = task.result()
        except asyncio.CancelledError:
            continue
        except Exception as e:
            logger.error("Source query failed: %s", e, exc_info=True)
            processed.append({
                "error": f"Task execution failed: {type(e).__name__}",
                "source": "internal",
                "status": "failed",
            })
            continue

        if isinstance(res, list):
            processed.extend(r for r in res if isinstance(r, dict))
        elif isinstance(res, dict):
            processed.append(res)
        elif res is not None:
            logger.warning("Unexpected result type from source query: %s", type(res))

    return processed
