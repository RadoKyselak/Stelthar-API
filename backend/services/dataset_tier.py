"""Dataset tier: claims the fixed metric list does not cover, answered from
government datasets instead of a web search.

  1. Candidates come from the agencies' own catalogs: every American
     Community Survey profile estimate (nation, states, counties and cities
     of 65,000+), and FRED's 800,000 series when FRED_API_KEY is set.
  2. One small model call picks the series that measures exactly what the
     claim states, or none. It never handles a number.
  3. The figure is read from the agency's API for the claim's place and
     period, and judged by the same deterministic rules as every other check.

A claim reaches this tier only when it states a figure, no fixed series
covers it, and the paid AI tier is configured. If nothing fits, the caller
moves on to web search.
"""
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import List, Optional, Set

import config
from claims.parser import ParsedClaim
from claims.verdict import HIGH, MEDIUM, NO_VERDICT, assess
from config import logger
from evidence import ai_search
from evidence.datasets import Candidate, census_profile, fred, picker
from evidence.sources import SourceUnavailable

TIER_DATASET = "dataset"
_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december")


def _drop_words(parsed: ParsedClaim) -> Set[str]:
    """Words that name the place or period, not the measure."""
    words = set(_MONTHS)
    if parsed.geography and parsed.geography.name:
        words |= {w.lower() for w in parsed.geography.name.replace(",", " ").split()}
    return words


async def verify(text: str, parsed: ParsedClaim, client_ip: Optional[str], started: float):
    from services.verify_v2 import _verdict_to_result  # avoid an import cycle

    if parsed.claimed_value is None or parsed.is_trend_claim or parsed.has_unresolved_period:
        return None
    # The pick is a model call: same switch, gate and limits as the AI tier.
    if not config.ANTHROPIC_API_KEY or config.AI_SEARCH_PROVIDER != "claude":
        return None
    if ai_search.gate(text, names_a_metric=True):
        return None

    geo = parsed.geography
    drop = _drop_words(parsed)
    want = parsed.claimed_value.family
    cands: List[Candidate] = []
    census_year = None
    census_geo = None
    try:
        annual_claim = not parsed.month and not parsed.quarter and parsed.year_basis != "fiscal"
        if census_profile.available() and annual_claim:
            years = [parsed.year] if parsed.year else [datetime.now(timezone.utc).year - y for y in (1, 2)]
            for y in years:
                if not await census_profile.catalog(y):
                    continue  # that ACS year is not published yet
                if geo is None or geo.level == "nation":
                    g = None
                elif geo.level == "state":
                    g = geo
                elif geo.name:
                    g = await census_profile.resolve_place(geo.name, y, text)
                    logger.info("Dataset place %r (%s) -> %s", geo.name, y, g.name if g else None)
                    if g is None:
                        break  # a place we cannot pin down: no Census answer
                else:
                    break
                found = await census_profile.candidates(text, y, drop, want, limit=30)
                if found:
                    cands += found
                    census_year, census_geo = y, g
                    break
        # FRED series here are national; a state or city claim is not offered them.
        if fred.available() and (geo is None or geo.level == "nation"):
            cands += await fred.candidates(text, drop, limit=12)
    except SourceUnavailable as e:
        logger.warning("Dataset catalog unavailable: %s", e)
    if not cands:
        return None

    where = (census_geo.name if census_geo else (geo.name if geo else "United States"))
    when = (f"{_MONTHS[parsed.month - 1].title()} {parsed.year}" if parsed.month and parsed.year
            else str(parsed.year or census_year or "latest available"))
    choice = await picker.pick(text, where, when, cands, client_ip)
    c = choice.candidate
    if c is None:
        return None

    try:
        if c.provider == "census_profile":
            dp = await census_profile.fetch(c, census_year, census_geo)
        else:
            dp = await fred.fetch(c, parsed.year, parsed.month, choice.stat)
    except SourceUnavailable as e:
        logger.warning("Dataset fetch failed for %s: %s", c.id, e)
        return None
    if dp is None:
        return None

    # A resolved city or county replaces the unresolved name the parser saw.
    judged = parsed
    if dp.geography.level in ("place", "county"):
        judged = replace(parsed, geography=dp.geography)
    v = assess(judged, [dp])
    result = _verdict_to_result(text, judged, v, started)
    result.tier = TIER_DATASET
    result.model_calls = choice.model_calls
    # A model chose the series, so the answer is never rated above Medium.
    if result.confidence == HIGH:
        result.confidence = MEDIUM
    result.notes.append(f"dataset: {c.provider} {c.id} ({choice.stat}); "
                        f"pick cost ${choice.cost_usd:.5f}")
    if result.verdict == NO_VERDICT:
        return None
    return result
