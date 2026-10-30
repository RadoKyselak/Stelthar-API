"""Mortgage rates and the Fed's target rate, read from FRED's public download.

FRED (Federal Reserve Bank of St. Louis) serves any series as CSV at
fredgraph.csv without an API key, so these two common claims are checked from
the published numbers with no key and no model call (plus the trade deficit
and the federal minimum wage, which FRED republishes from Census/BEA and DOL):

  MORTGAGE30US  Freddie Mac's weekly 30-year fixed rate (Primary Mortgage
                Market Survey), the figure news reports quote.
  DFEDTARU/L    The upper and lower limits of the Federal Reserve's target
                range for the federal funds rate, daily.

A month or a year is summarised the way the claim reads: "hit 7.79%" is the
highest week, "averaged 7%" the average, and "raised rates to 5.5%" the rate
in force at the end of the period.
"""
import calendar
import csv
import io
import re
from datetime import date, datetime, timezone
from typing import List, Optional, Tuple

import httpx

from config import logger
from evidence.cache import cached
from evidence.datapoint import ANNUAL, MONTHLY, POINT_IN_TIME, Datapoint, Geography, Period
from evidence.excerpt import make as make_excerpt, row as ex_row
from evidence.units import PERCENT, USD, Quantity

_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"
_DAY = 24 * 3600
_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")

_PEAK_RE = re.compile(r"\b(hit|hits|peaked|peak|reached|reaches|topped|tops|high|highest|"
                      r"record|climbed to|rose to|soared|jumped to|spiked)\b", re.IGNORECASE)
_LOW_RE = re.compile(r"\b(low|lowest|bottomed|fell to|dropped to|sank to)\b", re.IGNORECASE)
_AVG_RE = re.compile(r"\b(averag\w*|typical|mean)\b", re.IGNORECASE)

SERIES = {
    "mortgage_rate": {
        "ids": ("MORTGAGE30US",),
        "publisher": "Freddie Mac, Primary Mortgage Market Survey",
        "short": "Freddie Mac",
        "title": "30-year fixed-rate mortgage average",
        "units": "percent",
        "frequency": "weekly",
    },
    "fed_funds_rate": {
        "ids": ("DFEDTARU", "DFEDTARL"),
        "publisher": "Board of Governors of the Federal Reserve System",
        "short": "The Federal Reserve",
        "title": "Federal funds target range",
        "units": "percent",
        "frequency": "daily",
    },
    "trade_deficit": {
        "ids": ("BOPGSTB",),
        "publisher": "U.S. Census Bureau and U.S. Bureau of Economic Analysis",
        "short": "Census/BEA",
        "title": "Trade deficit in goods and services",
        "units": "millions of dollars",
        "frequency": "monthly",
    },
    "minimum_wage": {
        "ids": ("FEDMINNFRWG",),
        "publisher": "U.S. Department of Labor, Wage and Hour Division",
        "short": "The Labor Department",
        "title": "Federal minimum hourly wage",
        "units": "dollars per hour",
        "frequency": "monthly",
    },
}


async def _rows(series_id: str, start: date, end: date) -> List[Tuple[str, float]]:
    params = {"id": series_id, "cosd": start.isoformat(), "coed": end.isoformat()}

    async def load():
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            r = await client.get(_CSV, params=params)
            r.raise_for_status()
            out = []
            for rec in csv.reader(io.StringIO(r.text)):
                if len(rec) < 2 or not re.match(r"\d{4}-\d{2}-\d{2}$", rec[0]):
                    continue
                try:
                    out.append([rec[0], float(rec[1])])
                except ValueError:
                    continue  # "." marks a missing day
            return out

    recent = end >= date.today().replace(day=1)
    try:
        rows = await cached("fred_csv", f"{series_id}|{start}|{end}", _DAY if recent else 30 * _DAY, load)
    except httpx.HTTPError as e:
        logger.warning("FRED download failed for %s: %s", series_id, e)
        return []
    return [(d, v) for d, v in (rows or [])]


def _window(year: Optional[int], month: Optional[int]) -> Tuple[date, date]:
    if year and month:
        return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
    if year:
        return date(year, 1, 1), date(year, 12, 31)
    today = datetime.now(timezone.utc).date()
    return today.replace(year=today.year - 1), today


def _pick(rows, metric: str, claim: str, present: bool):
    """(value, the cited row or None, how it was summarised)."""
    if present:
        d, v = rows[-1]
        return v, (d, v), "latest"
    if _AVG_RE.search(claim) and metric == "mortgage_rate":
        avg = round(sum(v for _, v in rows) / len(rows), 2)
        return avg, None, "average"
    if _PEAK_RE.search(claim):
        d, v = max(rows, key=lambda r: r[1])
        return v, (d, v), "highest"
    if _LOW_RE.search(claim):
        d, v = min(rows, key=lambda r: r[1])
        return v, (d, v), "lowest"
    if metric == "fed_funds_rate":
        d, v = rows[-1]  # the rate in force at the end of the period
        return v, (d, v), "end"
    avg = round(sum(v for _, v in rows) / len(rows), 2)
    return avg, None, "average"


def _us_date(iso: str) -> str:
    y, m, d = (int(x) for x in iso.split("-"))
    return f"{_MONTHS[m - 1]} {d}, {y}"


def _point(metric, info, sid, value, family, period, as_of, raw, excerpt_rows, units, note=""):
    return Datapoint(
        quantity=Quantity(magnitude=value, family=family, raw=raw),
        metric=metric, label=info["title"], observed=period,
        geography=Geography.national(), source="FRED",
        source_url=f"https://fred.stlouisfed.org/series/{sid}", series_id=sid,
        as_of=as_of, raw_value=raw,
        extra={"original_source": info["publisher"], "publisher_short": info["short"], "units": units,
               "excerpt": make_excerpt(excerpt_rows, units=units, note=note)},
    )


async def _trade(metric, info, year, month, present) -> List[Datapoint]:
    """The trade deficit: the goods-and-services balance, negated. A year is the
    sum of its twelve monthly balances, as Census and BEA total it."""
    sid = info["ids"][0]
    start, end = _window(year, month)
    rows = await _rows(sid, start, end)
    if not rows:
        return []
    units = "millions of dollars; a negative balance is a deficit"
    if present or (year and month):
        d, v = rows[-1] if present else rows[0]
        y, m = int(d[:4]), int(d[5:7])
        period = Period(year=y, kind=MONTHLY, month=m,
                        basis="latest month" if present else "monthly balance")
        shown = [ex_row(f"{_MONTHS[m - 1]} {y}", f"{v:,.0f}", cited=True)]
        return [_point(metric, info, sid, -v * 1e6, USD, period, d, f"{-v:.0f}", shown, units,
                       note="Seasonally adjusted.")]
    if len(rows) < 12:
        return []  # the year is not complete yet
    total = sum(v for _, v in rows)
    shown = [ex_row(f"{_MONTHS[int(d[5:7]) - 1]} {d[:4]}", f"{v:,.0f}") for d, v in rows[-3:]]
    shown.append(ex_row(f"Total, {year}", f"{total:,.0f}", cited=True))
    period = Period(year=year, kind=ANNUAL, basis="sum of the twelve monthly balances")
    return [_point(metric, info, sid, -total * 1e6, USD, period, f"{year}-12-31", f"{-total:.0f}",
                   shown, units, note="Last three months shown; seasonally adjusted.")]


async def _minimum_wage(metric, info, year, month, present) -> List[Datapoint]:
    """The federal minimum wage in force at the end of the period, or now."""
    sid = info["ids"][0]
    start, end = _window(year, month)
    rows = await _rows(sid, start, end)
    if not rows:
        return []
    d, v = rows[-1]
    if present:
        period = Period(year=int(d[:4]), kind=POINT_IN_TIME, basis="latest observation", record_date=d)
    elif month:
        period = Period(year=year, kind=MONTHLY, month=month, basis="")
    else:
        period = Period(year=year, kind=ANNUAL, basis=f"in force on {_us_date(d)}")
    shown = [ex_row(_us_date(d), f"{v:.2f}", cited=True)]
    return [_point(metric, info, sid, v, USD, period, d, f"{v:.2f}", shown, "dollars per hour")]


async def fetch(metric: str, claim: str, claimed: Optional[float], year: Optional[int],
                month: Optional[int], present: bool) -> List[Datapoint]:
    info = SERIES.get(metric)
    if not info:
        return []
    if metric == "trade_deficit":
        return await _trade(metric, info, year, month, present)
    if metric == "minimum_wage":
        return await _minimum_wage(metric, info, year, month, present)
    start, end = _window(year, month)
    series = []
    for sid in info["ids"]:
        rows = await _rows(sid, start, end)
        if rows:
            series.append((sid, rows))
    if not series:
        return []

    # The target is a range: a claim of either limit is the Fed's rate.
    if metric == "fed_funds_rate" and claimed is not None and len(series) == 2:
        picks = [(sid, rows, _pick(rows, metric, claim, present)) for sid, rows in series]
        sid, rows, (value, cited, how) = min(picks, key=lambda p: abs(p[2][0] - claimed))
    else:
        sid, rows = series[0]
        value, cited, how = _pick(rows, metric, claim, present)

    limit = {"DFEDTARU": "upper limit", "DFEDTARL": "lower limit"}.get(sid, "")
    label = f"{info['title']}, {limit}" if limit else info["title"]

    if how == "latest":
        period = Period(year=int(cited[0][:4]), kind=POINT_IN_TIME, basis="latest observation",
                        record_date=cited[0])
    elif month and year:
        basis = {"highest": "highest week of the month" if info["frequency"] == "weekly" else "highest in the month",
                 "lowest": "lowest in the month", "average": "average of the weekly rates",
                 "end": f"in force on {_us_date(cited[0])}" if cited else ""}[how]
        period = Period(year=year, kind=MONTHLY, month=month, basis=basis)
    else:
        basis = {"highest": "highest in the year", "lowest": "lowest in the year",
                 "average": "annual average of the weekly rates",
                 "end": f"in force on {_us_date(cited[0])}" if cited else ""}[how]
        period = Period(year=year or start.year, kind=ANNUAL, basis=basis)

    # Show the published rows the figure comes from: the whole month, or the
    # few rows around the cited one for a year.
    if how == "average":
        shown = rows if len(rows) <= 6 else []
    else:
        i = rows.index(cited)
        shown = rows[max(0, i - 2): i + 3]
    if metric == "fed_funds_rate":
        shown = [r for r in shown if r == cited] or shown[-1:]
    excerpt_rows = [ex_row(_us_date(d), f"{v:.2f}", cited=(cited is not None and (d, v) == tuple(cited)))
                    for d, v in shown]
    note = ""
    if how == "average":
        # The average is the cited figure, under the weekly rows it comes from.
        excerpt_rows.append(ex_row(f"Average of {len(rows)} {info['frequency']} rates", f"{value:.2f}", cited=True))
    if metric == "fed_funds_rate":
        other = [s for s in series if s[0] != sid]
        if other and cited:
            od = dict(other[0][1]).get(cited[0])
            if od is not None:
                lo, hi = sorted([value, od])
                note = f"Target range on that date: {lo:.2f}% to {hi:.2f}%."

    return [Datapoint(
        quantity=Quantity(magnitude=value, family=PERCENT, raw=f"{value:.2f}"),
        metric=metric,
        label=label,
        observed=period,
        geography=Geography.national(),
        source="FRED",
        source_url=f"https://fred.stlouisfed.org/series/{sid}",
        series_id=sid,
        as_of=cited[0] if cited else end.isoformat(),
        raw_value=f"{value:.2f}",
        extra={"original_source": info["publisher"], "publisher_short": info["short"], "units": info["units"],
               "excerpt": make_excerpt(excerpt_rows, units="percent", note=note) if excerpt_rows or note else None},
    )]
