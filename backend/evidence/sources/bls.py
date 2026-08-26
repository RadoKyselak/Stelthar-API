"""BLS timeseries — labour and price statistics, with the basis stated.

Three things the previous adapter got wrong, all of them the same mistake in
different clothes: presenting a figure as covering a period it does not.

1. It requested `annualaverage: True` and looked for period M13. Verified
   against the live API: without a registration key BLS silently ignores that
   flag and returns no M13 rows at all. The adapter then fell through to "the
   most recent monthly point" and labelled it with the requested year — so
   December's rate was served as the answer to a question about the year.

2. For CPI it divided the current year's value by the prior year's, where the
   current value could be a single month and the prior one an annual average.
   Dividing a June index by a full-year average is not an inflation rate.

3. "Inflation in 2022" and "inflation hit 9% in June 2022" are different
   questions — annual-average-over-annual-average versus month-over-same-month.
   Only the second reaches 9%; conflating them makes a true claim look false.

This module computes the annual average itself when BLS does not supply one,
and always says which basis produced the number. That also means it works with
no API key, which keeps the whole tier free.
"""
import json
import statistics
from typing import Any, Dict, List, Optional

import httpx

from config import BLS_API_KEY, logger
from config.constants import API_TIMEOUTS, RATE_LIMIT_QUOTAS
from evidence.cache import TTL_IMMUTABLE, TTL_VOLATILE, cached
from evidence.sources import SourceUnavailable
from evidence.datapoint import ANNUAL, MONTHLY, Datapoint, Geography, Period
from evidence.units import INDEX, JOBS, PERCENT, USD, Quantity, parse_number
from utils.rate_limiter import get_quota_limiter

_limiter = get_quota_limiter("BLS", *RATE_LIMIT_QUOTAS.BLS)
_ENDPOINT = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
from datetime import datetime as _dt, timezone as _tz
_CURRENT_YEAR = _dt.now(_tz.utc).year

# BLS publishes employment levels in thousands.
_THOUSANDS_SERIES = {"CES0000000001"}


def series_permalink(series_id: str) -> str:
    return f"https://data.bls.gov/timeseries/{series_id}"


async def _fetch_raw(series_id: str, start_year: int, end_year: int) -> List[Dict[str, Any]]:
    """Cached. Keyless BLS allows 25 requests/day, which a single eval run
    exhausts; a closed year's observations never change, so re-fetching them is
    pure waste of a scarce quota."""
    ttl = TTL_VOLATILE if end_year >= _CURRENT_YEAR else TTL_IMMUTABLE
    return await cached(
        "bls", f"{series_id}:{start_year}:{end_year}", ttl,
        lambda: _fetch_raw_uncached(series_id, start_year, end_year),
    )


async def _fetch_raw_uncached(series_id: str, start_year: int, end_year: int) -> List[Dict[str, Any]]:
    await _limiter.acquire()
    body: Dict[str, Any] = {
        "seriesid": [series_id],
        "startyear": str(start_year),
        "endyear": str(end_year),
        "annualaverage": True,
    }
    # Optional: without it BLS serves the lower-quota v1 tier, which still works
    # but never returns M13 annual averages. We compute those ourselves.
    if BLS_API_KEY:
        body["registrationKey"] = BLS_API_KEY

    try:
        async with httpx.AsyncClient(timeout=API_TIMEOUTS.BLS) as client:
            r = await client.post(
                _ENDPOINT, headers={"Content-Type": "application/json"},
                content=json.dumps(body),
            )
            r.raise_for_status()
            payload = r.json()
    except httpx.HTTPError as e:
        raise SourceUnavailable("BLS", f"request failed: {e}") from e
    except json.JSONDecodeError as e:
        raise SourceUnavailable("BLS", "non-JSON response") from e

    if payload.get("status") != "REQUEST_SUCCEEDED":
        msg = "; ".join(payload.get("message") or []) or str(payload.get("status"))
        # A daily-threshold refusal is an outage on our side, not evidence that
        # the agency published nothing for the year.
        raise SourceUnavailable("BLS", msg)
    series = payload.get("Results", {}).get("series", [])
    return series[0].get("data", []) if series else []


def _monthly_points(rows: List[Dict[str, Any]], year: int) -> List[Dict[str, Any]]:
    return [r for r in rows if r.get("year") == str(year)
            and str(r.get("period", "")).startswith("M")
            and r.get("period") != "M13"]


def _published_annual(rows: List[Dict[str, Any]], year: int) -> Optional[Dict[str, Any]]:
    for r in rows:
        if r.get("year") == str(year) and r.get("period") == "M13":
            return r
    return None


def _annual_value(rows: List[Dict[str, Any]], year: int):
    """Return (value, basis, n_months) for a year, or (None, reason, n).

    Prefers BLS's own published annual average. Falls back to the mean of the
    monthly observations, and refuses to call a partial year an annual figure.
    """
    published = _published_annual(rows, year)
    if published is not None:
        v = parse_number(published.get("value"))
        if v is not None:
            return v, "BLS published annual average", 12

    months = _monthly_points(rows, year)
    raws = [str(m.get("value") or "") for m in months]
    values = [parse_number(m.get("value")) for m in months]
    values = [v for v in values if v is not None]
    if len(values) == 12:
        # Report to the precision BLS itself publishes. The raw mean of twelve
        # one-decimal rates carries three decimals of false precision, and that
        # spurious precision is enough to make a correctly-quoted claim look
        # wrong when compared digit for digit.
        decimals = max((len(r.split(".", 1)[1]) for r in raws if "." in r), default=1)
        return (round(statistics.fmean(values), decimals),
                f"computed as the mean of 12 monthly observations, to {decimals} dp", 12)
    if values:
        return None, f"only {len(values)} of 12 months published for {year}", len(values)
    return None, f"no data published for {year}", 0


def _scale_for(series_id: str, value: float) -> float:
    return value * 1000.0 if series_id in _THOUSANDS_SERIES else value


async def fetch_annual(
    metric: str, label: str, series_id: str, family: str, year: int,
) -> List[Datapoint]:
    """One annual-average datapoint, or nothing if the year is incomplete.

    Returning nothing for a partial year is deliberate. The alternative — the
    old behaviour — was to return the latest monthly value under the requested
    year's label, which reads as an annual figure and is not one.
    """
    rows = await _fetch_raw(series_id, year, year)
    value, basis, n = _annual_value(rows, year)
    if value is None:
        logger.info("BLS %s: %s", series_id, basis)
        return []
    return [Datapoint(
        quantity=Quantity(magnitude=_scale_for(series_id, value), family=family),
        metric=metric, label=label,
        observed=Period(year=year, kind=ANNUAL, basis=basis),
        geography=Geography.national(),
        source="BLS", source_url=series_permalink(series_id),
        series_id=series_id, raw_value=f"{value}",
        extra={"months_observed": n},
    )]


async def fetch_monthly(
    metric: str, label: str, series_id: str, family: str, year: int, month: int,
) -> List[Datapoint]:
    rows = await _fetch_raw(series_id, year, year)
    target = f"M{month:02d}"
    for r in rows:
        if r.get("year") == str(year) and r.get("period") == target:
            v = parse_number(r.get("value"))
            if v is None:
                break
            return [Datapoint(
                quantity=Quantity(magnitude=_scale_for(series_id, v), family=family),
                metric=metric, label=label,
                observed=Period(year=year, kind=MONTHLY, month=month,
                                basis=f"{r.get('periodName', target)} monthly observation"),
                geography=Geography.national(),
                source="BLS", source_url=series_permalink(series_id),
                series_id=series_id, raw_value=str(r.get("value")),
            )]
    return []


async def fetch_inflation(
    metric: str, label: str, series_id: str, year: int, month: Optional[int] = None,
) -> List[Datapoint]:
    """Year-over-year percent change from a price index.

    Annual basis compares annual average to annual average. Monthly basis
    compares a month to the SAME month a year earlier. Mixing the two — which is
    what produced audit finding #9 — is what makes a true "9% in June 2022"
    claim look wrong.
    """
    rows = await _fetch_raw(series_id, year - 1, year)

    if month is not None:
        cur = prev = None
        target = f"M{month:02d}"
        for r in rows:
            if r.get("period") != target:
                continue
            if r.get("year") == str(year):
                cur = parse_number(r.get("value"))
            elif r.get("year") == str(year - 1):
                prev = parse_number(r.get("value"))
        if cur is None or not prev:
            return []
        pct = (cur - prev) / prev * 100.0
        basis = f"{target} {year} vs {target} {year - 1}, not seasonally adjusted"
        period = Period(year=year, kind=MONTHLY, month=month, basis=basis)
    else:
        cur, cur_basis, _ = _annual_value(rows, year)
        prev, _, _ = _annual_value(rows, year - 1)
        if cur is None or not prev:
            logger.info("BLS inflation %s: %s", series_id, cur_basis)
            return []
        pct = (cur - prev) / prev * 100.0
        period = Period(
            year=year, kind=ANNUAL,
            basis=f"annual average vs {year - 1} annual average ({cur_basis})",
        )

    return [Datapoint(
        quantity=Quantity(magnitude=pct, family=PERCENT),
        metric=metric, label=label,
        observed=period,
        geography=Geography.national(),
        source="BLS", source_url=series_permalink(series_id),
        series_id=series_id, raw_value=f"{pct:.2f}",
        extra={"index_current": cur, "index_prior": prev},
    )]


async def fetch_series(
    metric: str, label: str, series_id: str, family: str,
    start_year: int, end_year: int,
) -> List[Datapoint]:
    """Annual values across a range — one request, the input to trend checks."""
    rows = await _fetch_raw(series_id, start_year, end_year)
    out: List[Datapoint] = []
    for year in range(start_year, end_year + 1):
        value, basis, n = _annual_value(rows, year)
        if value is None:
            continue
        out.append(Datapoint(
            quantity=Quantity(magnitude=_scale_for(series_id, value), family=family),
            metric=metric, label=label,
            observed=Period(year=year, kind=ANNUAL, basis=basis),
            geography=Geography.national(),
            source="BLS", source_url=series_permalink(series_id),
            series_id=series_id, raw_value=f"{value}",
            extra={"months_observed": n},
        ))
    return out
