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
from evidence.excerpt import make as make_excerpt, row as ex_row, units_for
from evidence.units import INDEX, JOBS, PERCENT, USD, Quantity, parse_number
from utils.rate_limiter import get_quota_limiter

_limiter = get_quota_limiter("BLS", *RATE_LIMIT_QUOTAS.BLS)
_ENDPOINT = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
from datetime import datetime as _dt, timezone as _tz
_CURRENT_YEAR = _dt.now(_tz.utc).year

# BLS publishes employment levels in thousands.
_THOUSANDS_SERIES = {"CES0000000001", "CEU0000000001"}


def series_permalink(series_id: str, *, annual: bool = False, pct12: bool = False,
                     year: Optional[int] = None) -> str:
    """A data.bls.gov link that opens on the figure being cited.

    The default series page shows monthly values for the last ten years only:
    no annual average, no 12-month change. A reader following a citation for
    "2023 annual average" or "12-month change, June 2022" has to see that
    number, so the view is requested explicitly.
    """
    params = []
    if pct12:
        params.append("output_view=pct_12mths")
    if annual:
        params.append("annualAveragesRequested=true")
    if year and year < _dt.now(_tz.utc).year - 9:
        params.append(f"years_option=specific_years&from_year={year - 1}&to_year={year}")
    base = f"https://data.bls.gov/timeseries/{series_id}"
    return base + ("?" + "&".join(params) if params else "")


def annual_series_for(series_id: str) -> str:
    """The series BLS publishes official annual averages for.

    BLS publishes annual averages of the CPS (LNS1… → LNU0…) and of payroll
    employment and earnings (CES… → CEU…) from the NOT seasonally adjusted
    series. The mean of the adjusted months can be off: $33.70 against the
    published $33.72 for 2023 average hourly earnings.
    """
    sid = (series_id or "").upper()
    if sid.startswith("LNS1"):
        return "LNU0" + sid[4:]
    if sid.startswith("CES"):
        return "CEU" + sid[3:]
    return series_id


def geography_for_series(series_id: str) -> Geography:
    """Where a BLS series measures, read from the series id itself.

    LAUS state series encode the state FIPS code in positions 5-6
    (LAUST48… / LASST48… = Texas). Everything else wired up here is national.
    """
    sid = (series_id or "").upper()
    if sid.startswith(("LAUST", "LASST")) and len(sid) >= 7 and sid[5:7].isdigit():
        from claims.geography import _STATE_FIPS
        fips = sid[5:7]
        name = next((n for n, f in _STATE_FIPS.items() if f == fips), f"FIPS {fips}")
        return Geography(name=name.title(), level="state", fips=fips)
    return Geography.national()


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


# ---- Excerpts: the cells a figure was read from, laid out as BLS tables are.
_MON = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _units(series_id: str, family: str) -> str:
    return "thousands of jobs" if series_id in _THOUSANDS_SERIES else units_for(family)


def _month_row(rows: List[Dict[str, Any]], year: int, month: int) -> Optional[Dict[str, Any]]:
    target = f"M{month:02d}"
    return next((r for r in rows if r.get("year") == str(year) and r.get("period") == target), None)


def _decimals(rows: List[Dict[str, Any]], year: int) -> int:
    raws = [str(m.get("value") or "") for m in _monthly_points(rows, year)]
    return max((len(r.split(".", 1)[1]) for r in raws if "." in r), default=1)


def _annual_excerpt(rows, year: int, value: float, series_id: str, family: str):
    out = []
    dec = _month_row(rows, year, 12)
    if dec is not None:
        out.append(ex_row(f"{year} Dec", dec.get("value")))
    pub = _published_annual(rows, year)
    note = ""
    if pub is not None:
        out.append(ex_row(f"{year} Annual", pub.get("value"), cited=True))
    else:
        out.append(ex_row(f"{year} Annual average", f"{value:.{_decimals(rows, year)}f}", cited=True))
        note = ("BLS returned the 12 monthly values; the annual average is their mean, "
                "rounded as BLS rounds.")
    return make_excerpt(out, _units(series_id, family), note)


def _monthly_excerpt(rows, year: int, month: int, series_id: str, family: str):
    out = []
    for m in (month - 1, month, month + 1):
        if 1 <= m <= 12:
            r = _month_row(rows, year, m)
            if r is not None and r.get("value") not in (None, "", "-"):
                out.append(ex_row(f"{year} {_MON[m - 1]}", r.get("value"), cited=m == month))
    return make_excerpt(out, _units(series_id, family))


async def fetch_annual(
    metric: str, label: str, series_id: str, family: str, year: int,
) -> List[Datapoint]:
    """One annual-average datapoint, or nothing if the year is incomplete.

    Returning nothing for a partial year is deliberate. The alternative — the
    old behaviour — was to return the latest monthly value under the requested
    year's label, which reads as an annual figure and is not one.
    """
    # BLS publishes CPS annual averages from the NOT seasonally adjusted series
    # (LNU04…), not the adjusted one (LNS14…) quoted monthly. Averaging the
    # adjusted months can land 0.1 point off the official annual figure.
    series_id = annual_series_for(series_id)
    rows = await _fetch_raw(series_id, year, year)
    value, basis, n = _annual_value(rows, year)
    if value is None:
        logger.info("BLS %s: %s", series_id, basis)
        return []
    return [Datapoint(
        quantity=Quantity(magnitude=_scale_for(series_id, value), family=family),
        metric=metric, label=label,
        observed=Period(year=year, kind=ANNUAL, basis=basis),
        geography=geography_for_series(series_id),
        source="BLS", source_url=series_permalink(series_id, annual=True, year=year),
        series_id=series_id, raw_value=f"{value}",
        extra={"months_observed": n,
               "excerpt": _annual_excerpt(rows, year, value, series_id, family)},
    )]


_MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July",
                "August", "September", "October", "November", "December")


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
                geography=geography_for_series(series_id),
                source="BLS", source_url=series_permalink(series_id, year=year),
                series_id=series_id, raw_value=str(r.get("value")),
                extra={"excerpt": _monthly_excerpt(rows, year, month, series_id, family)},
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
        cur_raw = prev_raw = ""
        target = f"M{month:02d}"
        for r in rows:
            if r.get("period") != target:
                continue
            if r.get("year") == str(year):
                cur, cur_raw = parse_number(r.get("value")), str(r.get("value"))
            elif r.get("year") == str(year - 1):
                prev, prev_raw = parse_number(r.get("value")), str(r.get("value"))
        if cur is None or not prev:
            return []
        pct = (cur - prev) / prev * 100.0
        basis = f"12-month change from {_MONTH_NAMES[month - 1]} {year - 1}, not seasonally adjusted"
        period = Period(year=year, kind=MONTHLY, month=month, basis=basis)
        labels = (f"Index, {_MON[month - 1]} {year - 1}", f"Index, {_MON[month - 1]} {year}")
    else:
        cur, cur_basis, _ = _annual_value(rows, year)
        prev, _, _ = _annual_value(rows, year - 1)
        if cur is None or not prev:
            logger.info("BLS inflation %s: %s", series_id, cur_basis)
            return []
        pct = (cur - prev) / prev * 100.0
        period = Period(
            year=year, kind=ANNUAL,
            basis=f"change in annual average from {year - 1}",
        )
        cur_raw = f"{cur:.{_decimals(rows, year)}f}"
        prev_raw = f"{prev:.{_decimals(rows, year - 1)}f}"
        labels = (f"Index, {year - 1} annual average", f"Index, {year} annual average")

    # BLS publishes 12-month changes to one decimal, computed from the index
    # exactly as here ("9.1 percent" for June 2022). The comparison and the
    # citation use that published figure, not an unrounded 9.06 BLS never wrote.
    published = round(pct + 0.0, 1)
    excerpt = make_excerpt(
        [ex_row(labels[0], prev_raw), ex_row(labels[1], cur_raw),
         ex_row("Change (%)", f"{published:.1f}", cited=True)],
        note=(f"({cur_raw} − {prev_raw}) ÷ {prev_raw} × 100 = {pct:.2f}, "
              f"rounded to one decimal as BLS publishes it."),
    )
    return [Datapoint(
        quantity=Quantity(magnitude=published, family=PERCENT),
        metric=metric, label=label,
        observed=period,
        geography=geography_for_series(series_id),
        source="BLS", source_url=series_permalink(series_id, pct12=True, annual=month is None,
                                                  year=year),
        series_id=series_id, raw_value=f"{published:.1f}",
        extra={"index_current": cur, "index_prior": prev, "unrounded_change": round(pct, 4),
               "excerpt": excerpt},
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
            geography=geography_for_series(series_id),
            source="BLS", source_url=series_permalink(series_id, annual=True),
            series_id=series_id, raw_value=f"{value}",
            extra={"months_observed": n},
        ))
    return out


async def fetch_latest_monthly(
    metric: str, label: str, series_id: str, family: str,
) -> List[Datapoint]:
    """The most recent monthly observation — what a present-tense price claim
    ("gas is $3 a gallon") is about. Searches this year and last."""
    year = _dt.now(_tz.utc).year
    rows = await _fetch_raw(series_id, year - 1, year)
    monthly = [r for r in rows if str(r.get("period", "")).startswith("M")
               and r.get("period") != "M13" and parse_number(r.get("value")) is not None]
    if not monthly:
        return []
    latest = max(monthly, key=lambda r: (int(r["year"]), r["period"]))
    y, mth = int(latest["year"]), int(latest["period"][1:])
    v = parse_number(latest["value"])
    return [Datapoint(
        quantity=Quantity(magnitude=_scale_for(series_id, v), family=family),
        metric=metric, label=label,
        observed=Period(year=y, kind=MONTHLY, month=mth,
                        basis="latest monthly observation"),
        geography=geography_for_series(series_id),
        source="BLS", source_url=series_permalink(series_id),
        series_id=series_id, raw_value=str(latest.get("value")),
        extra={"excerpt": _monthly_excerpt(rows, y, mth, series_id, family)},
    )]


async def fetch_jobs_change(
    metric: str, label: str, series_id: str, year: int, month: Optional[int] = None,
) -> List[Datapoint]:
    """Jobs added (or lost): the change in payrolls, as the jobs report counts it.

    A month is that month against the one before; a year is December against
    the previous December. Both levels are shown, so the subtraction is visible.
    """
    if month:
        prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
    else:
        prev_y, prev_m, month = year - 1, 12, 12
    rows = await _fetch_raw(series_id, prev_y, year)
    a, b = _month_row(rows, prev_y, prev_m), _month_row(rows, year, month)
    va = parse_number(a.get("value")) if a else None
    vb = parse_number(b.get("value")) if b else None
    if va is None or vb is None:
        return []
    change = vb - va  # thousands, as published
    yearly = prev_m == 12 and month == 12 and prev_y == year - 1
    basis = (f"change from December {prev_y} to December {year}" if yearly
             else f"change from {_MON[prev_m - 1]} {prev_y} to {_MON[month - 1]} {year}")
    excerpt = make_excerpt([
        ex_row(f"{_MON[prev_m - 1]} {prev_y}", a.get("value")),
        ex_row(f"{_MON[month - 1]} {year}", b.get("value")),
        ex_row("Change", f"{change:+.0f}".replace("+", "+") if abs(change) >= 1 else f"{change:+.1f}", cited=True),
    ], units="thousands of jobs", note="Seasonally adjusted.")
    return [Datapoint(
        quantity=Quantity(magnitude=change * 1000.0, family=JOBS),
        metric=metric, label=label,
        observed=(Period(year=year, kind=ANNUAL, basis=basis) if yearly
                  else Period(year=year, kind=MONTHLY, month=month, basis=basis)),
        geography=geography_for_series(series_id),
        source="BLS", source_url=series_permalink(series_id, year=year),
        series_id=series_id, raw_value=f"{change * 1000:.0f}",
        extra={"excerpt": excerpt},
    )]
