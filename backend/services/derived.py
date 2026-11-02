"""Computed checks: claims that no single published number answers, worked out
from the agency's whole series. Free, keyless, no model call.

A web search finds sentences other people wrote. These claims need the
numbers themselves, the full series and, for revisions, what the agency
first published:

  direction     "The economy shrank in Q1 2026", "The U.S. lost jobs in July"
  change since  "Prices have risen more than 25% since January 2021"
  record        "Unemployment is the highest in a decade", "lowest since 1969"
  first print   a figure that matched the first report and was revised later
  census count  "The 2020 Census counted 331.4 million people"
  debt measure  "Debt held by the public is $40 trillion" (it is $32.7T)

Every figure comes from FRED's public download (fredgraph.csv), its archive of
earlier releases (ALFRED, alfredgraph.csv with vintage_date), or Treasury's
Fiscal Data API. Each check either recognises the claim's wording exactly and
answers it, or returns None and the normal pipeline runs.
"""
import calendar
import csv
import io
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional, Tuple

import httpx

from claims.verdict import (CONTRADICTED, HIGH, MEDIUM, MISLEADING, MOSTLY_ACCURATE, SUPPORTED,
                            Verdict)
from config import logger
from evidence.cache import cached
from evidence.datapoint import (MONTHLY, POINT_IN_TIME, QUARTERLY, Datapoint, Geography,
                                Period)
from evidence.excerpt import make as make_excerpt, row as ex_row
from evidence.units import INDEX, JOBS, PERCENT, PERSONS, USD, Quantity, humanize

_FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv"
_ALFRED = "https://alfred.stlouisfed.org/graph/alfredgraph.csv"
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December")
_MONTH_RE = "|".join(_MONTHS) + "|" + "|".join(m[:3] + r"\.?" for m in _MONTHS)
_BLS = ("U.S. Bureau of Labor Statistics", "BLS")
_BEA = ("U.S. Bureau of Economic Analysis", "BEA")


@dataclass(frozen=True)
class Series:
    sid: str
    freq: str        # "M" monthly, "Q" quarterly
    family: str
    title: str
    publisher: Tuple[str, str]
    units: str


UNRATE = Series("UNRATE", "M", PERCENT, "Unemployment rate", _BLS, "percent, seasonally adjusted")
PAYEMS = Series("PAYEMS", "M", JOBS, "Total nonfarm payroll employment", _BLS,
                "thousands of jobs, seasonally adjusted")
CPI_SA = Series("CPIAUCSL", "M", INDEX, "Consumer Price Index for All Urban Consumers (CPI-U)",
                _BLS, "index, 1982-84=100, seasonally adjusted")
CPI_NSA = Series("CUUR0000SA0", "M", INDEX, "Consumer Price Index for All Urban Consumers (CPI-U)",
                 _BLS, "index, 1982-84=100, not seasonally adjusted")
GDP = Series("A191RL1Q225SBEA", "Q", PERCENT, "Real gross domestic product, percent change", _BEA,
             "percent change at a seasonally adjusted annual rate")


def fred_series(sid: str, title: str = "", units: str = "") -> Series:
    """Any FRED series, for the engine. Units are what the interpreter or the
    caller says; the figure itself always comes from FRED."""
    sid = re.sub(r"[^A-Z0-9_]", "", sid.upper())[:30]
    fam = PERCENT if re.search(r"percent|rate", units, re.I) else USD if re.search(r"dollar", units, re.I) \
        else INDEX
    return Series(sid, "M", fam, title or sid, ("Federal Reserve Bank of St. Louis (FRED)", "FRED"),
                  units or "as published by FRED")


# ------------------------------------------------------------------ downloads

async def _rows(s: Series, start: Optional[date] = None, end: Optional[date] = None,
                vintage: Optional[date] = None) -> List[Tuple[date, float]]:
    """[(observation date, value)] from FRED, or from ALFRED as published on
    `vintage`. A blank value (a month with no release) is skipped."""
    params = {"id": s.sid}
    if start:
        params["cosd"] = start.isoformat()
    if end:
        params["coed"] = end.isoformat()
    if vintage:
        params["vintage_date"] = vintage.isoformat()
    url = _ALFRED if vintage else _FRED

    async def load():
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as c:
            r = await c.get(url, params=params)
            r.raise_for_status()
        out = []
        for rec in csv.reader(io.StringIO(r.text)):
            if len(rec) < 2 or not re.match(r"\d{4}-\d{2}-\d{2}$", rec[0]):
                continue
            try:
                out.append([rec[0], float(rec[1])])
            except ValueError:
                continue
        return out

    key = "|".join(f"{k}={v}" for k, v in sorted(params.items()))
    try:
        got = await cached("derived_fred", key, 30 * 86400 if vintage else 6 * 3600, load)
    except httpx.HTTPError as e:
        logger.warning("FRED/ALFRED download failed for %s: %s", s.sid, e)
        return []
    return [(date.fromisoformat(d), v) for d, v in (got or [])]


async def _bls_monthly(series_id: str, start_year: int, end_year: int) -> List[Tuple[date, float]]:
    """A BLS series by month, from the BLS API in the 20-year pieces it allows.
    Used for the CPI-U as BLS headlines it (not seasonally adjusted), which
    FRED does not always serve."""
    from evidence.sources import bls as bls_source
    out = {}
    y = max(start_year, end_year - 60)
    while y <= end_year:
        from config import BLS_API_KEY
        hi = min(end_year, y + (19 if BLS_API_KEY else 9))  # keyless BLS serves 10 years
        for r in await bls_source._fetch_raw(series_id, y, hi):
            p = r.get("period", "")
            if re.fullmatch(r"M(0[1-9]|1[0-2])", p):
                try:
                    out[date(int(r["year"]), int(p[1:]), 1)] = float(r["value"])
                except (KeyError, ValueError):
                    continue
        y = hi + 1
    return sorted(out.items())


def _end_of(obs: date, freq: str) -> date:
    if freq == "Q":
        m = obs.month + 2
        return date(obs.year, m, calendar.monthrange(obs.year, m)[1])
    return date(obs.year, obs.month, calendar.monthrange(obs.year, obs.month)[1])


async def first_release(s: Series, obs: date) -> Optional[Tuple[float, date]]:
    """The value as the agency first published it, and the archive date read.

    Jobs and unemployment come out about a week after the month ends, GDP's
    advance estimate about a month after the quarter. The first archive date
    that has the observation holds the first print; later revisions come a
    month (jobs) or two (GDP) after it."""
    steps = (31, 40, 50, 60, 75) if s.freq == "Q" else (12, 25, 40)
    today = datetime.now(timezone.utc).date()
    for days in steps:
        vd = _end_of(obs, s.freq) + timedelta(days=days)
        if vd >= today:
            return None
        rows = await _rows(s, obs, obs, vintage=vd)
        hit = [v for d, v in rows if d == obs]
        if hit:
            return hit[0], vd
    return None


# ------------------------------------------------------------------ wording

def _num(s: str) -> float:
    return float(s.replace(",", ""))


def _when(d: date, freq: str) -> str:
    if freq == "Q":
        return f"Q{(d.month - 1) // 3 + 1} {d.year}"
    return f"{_MONTHS[d.month - 1]} {d.year}"


def _month_of(token: str) -> int:
    t = token.strip(". ").lower()[:3]
    return [m[:3].lower() for m in _MONTHS].index(t) + 1


_SINCE_RE = re.compile(rf"\bsince\s+(?:(?:the\s+)?(?:start|beginning)\s+of\s+)?(?:({_MONTH_RE})\s+)?((?:19|20)\d\d)\b",
                       re.I)
_QUARTER_RE = re.compile(r"\b(?:Q([1-4])|(first|second|third|fourth|1st|2nd|3rd|4th)\s+quarter)"
                         r"(?:\s+of)?\s*,?\s*((?:19|20)\d\d)\b", re.I)
_ORD = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4}
_MONTH_YEAR_RE = re.compile(rf"\b({_MONTH_RE})\s*,?\s*((?:19|20)\d\d)\b", re.I)


def _subject_period(text: str) -> Tuple[Optional[date], str]:
    """The month or quarter the claim is about, ignoring any "since ..." clause.
    (date of the first day, "M" or "Q"), or (None, "") when it names none."""
    body = _SINCE_RE.sub(" ", text)
    q = _QUARTER_RE.search(body)
    if q:
        n = int(q.group(1)) if q.group(1) else _ORD[q.group(2).lower()]
        return date(int(q.group(3)), 3 * n - 2, 1), "Q"
    m = _MONTH_YEAR_RE.search(body)
    if m:
        return date(int(m.group(2)), _month_of(m.group(1)), 1), "M"
    return None, ""


def _topic(text: str) -> str:
    low = text.lower()
    if re.search(r"\bunemploy|\bjobless rate\b", low):
        return "unemployment"
    if re.search(r"\binflation\b", low):
        return "inflation"
    if re.search(r"\b(consumer )?prices\b|\bcost of living\b|\bcpi\b", low):
        return "prices"
    if re.search(r"\b(jobs?|payrolls?)\b", low):
        return "jobs"
    if re.search(r"\b(economy|gdp|economic output)\b", low):
        return "gdp"
    return ""


def _datapoint(s: Series, d: date, value: float, raw: str, excerpt, *, metric: str,
               label: str = "", basis: str = "", vintage: Optional[date] = None) -> Datapoint:
    if s.freq == "Q":
        period = Period(year=d.year, kind=QUARTERLY, quarter=(d.month - 1) // 3 + 1, basis=basis)
    else:
        period = Period(year=d.year, kind=MONTHLY, month=d.month, basis=basis)
    url = f"https://fred.stlouisfed.org/series/{s.sid}"
    source = "FRED"
    if vintage:
        url = f"https://alfred.stlouisfed.org/series?seid={s.sid}"
    if s.sid.startswith("CUUR"):  # read from the BLS API itself
        url, source = f"https://data.bls.gov/timeseries/{s.sid}", "BLS"
    return Datapoint(
        quantity=Quantity(magnitude=value, family=s.family if s.family != INDEX else PERCENT, raw=raw),
        metric=metric, label=label or s.title, observed=period, geography=Geography.national(),
        source=source, source_url=url, series_id=s.sid,
        as_of=vintage.isoformat() if vintage else None, raw_value=raw,
        extra={"original_source": s.publisher[0], "publisher_short": s.publisher[1],
               "units": s.units, "excerpt": excerpt},
    )


def _verdict(outcome: str, headline: str, claimed: str, official: str, dps, caveats=None,
             comparator: str = "equals") -> Verdict:
    return Verdict(verdict=outcome, confidence=HIGH if not caveats else MEDIUM,
                   headline=headline, claimed_display=claimed, official_display=official,
                   evidence=list(dps), caveats=list(caveats or []), comparator=comparator)


# ------------------------------------------------------------------ direction

_SHRANK_RE = re.compile(r"\b(shr[au]nk|contract(?:ed|s)|declined|fell|shrinks|went into reverse)\b", re.I)
_GREW_RE = re.compile(r"\b(grew|expanded|grows|expands)\b", re.I)
_LOST_JOBS_RE = re.compile(r"\b(lost|shed|cut|eliminated|losing|shedding)\s+(?:\w+\s+)?jobs\b"
                          r"|\bjobs?\s+(?:fell|declined|dropped|shrank)\b"
                          r"|\bpayrolls?\s+(?:fell|declined|dropped|shrank|contracted)\b", re.I)
_ADDED_JOBS_RE = re.compile(r"\b(added|gained|created|adding|gaining|creating)\s+(?:\w+\s+)?jobs\b"
                            r"|\bpayrolls?\s+(?:rose|grew|increased|expanded)\b", re.I)
_ANY_NUMBER_RE = re.compile(r"\d[\d,.]*\s*(%|percent|million|billion|thousand|k\b)|\$\s*\d", re.I)


async def _direction(text: str) -> Optional[Verdict]:
    """The economy shrank / grew in a quarter; the U.S. lost / added jobs in a
    month. No figure is claimed, only a sign, so no published sentence states
    it in the claim's terms; the series does."""
    if _ANY_NUMBER_RE.search(_SINCE_RE.sub(" ", _QUARTER_RE.sub(" ", _MONTH_YEAR_RE.sub(" ", text)))):
        return None  # it claims a figure: the normal check handles that
    topic = _topic(text)
    obs, freq = _subject_period(text)
    if obs is None:
        return None
    if topic == "gdp" and freq == "Q":
        down = bool(_SHRANK_RE.search(text))
        if not down and not _GREW_RE.search(text):
            return None
        return await op_direction("gdp", obs, down)
    if topic == "jobs" and freq == "M":
        lost = bool(_LOST_JOBS_RE.search(text))
        if not lost and not _ADDED_JOBS_RE.search(text):
            return None
        return await op_direction("jobs", obs, lost)
    return None


async def op_direction(topic: str, obs: date, down: bool) -> Optional[Verdict]:
    """Engine: did the measure fall (down=True) or rise in the period starting
    `obs`? "gdp" is the sign of real GDP growth that quarter; "jobs" the change
    in payrolls that month."""
    if topic == "gdp":
        rows = await _rows(GDP, obs, obs)
        if not rows:
            return None
        _, v = rows[0]
        said = "shrank" if down else "grew"
        right = (v < 0) if down else (v > 0)
        did = "shrank" if v < 0 else ("grew" if v > 0 else "was flat")
        first = await first_release(GDP, obs)
        when = _when(obs, "Q")
        shown = [ex_row(when, f"{v:.1f}", cited=True)]
        caveats = []
        if first and (first[0] < 0) != (v < 0):
            caveats.append(f"BEA's first estimate ({first[1]:%B %-d, %Y} archive) was {first[0]:+.1f}%, "
                           f"so the economy first looked like it {'shrank' if first[0] < 0 else 'grew'}.")
            shown.append(ex_row(f"{when}, first estimate", f"{first[0]:.1f}"))
        elif first and abs(first[0] - v) >= 0.1:
            shown.append(ex_row(f"{when}, first estimate", f"{first[0]:.1f}"))
        dp = _datapoint(GDP, obs, v, f"{v:.1f}", make_excerpt(shown, units=GDP.units),
                        metric="real_gdp_growth", basis="latest estimate")
        head = (f"Claim says the economy {said} in {when}. BEA reports real GDP "
                f"{'fell' if v < 0 else 'rose'} at a {abs(v):.1f}% annual rate: it {did}.")
        return _verdict(SUPPORTED if right else CONTRADICTED, head, said, f"{v:+.1f}%", [dp], caveats)

    if topic == "jobs":
        lost = down
        prev = date(obs.year - (obs.month == 1), (obs.month - 2) % 12 + 1, 1)
        rows = await _rows(PAYEMS, prev, obs)
        vals = {d: v for d, v in rows}
        if obs not in vals or prev not in vals:
            return None
        change = round(vals[obs] - vals[prev])
        right = change < 0 if lost else change > 0
        said = "lost jobs" if lost else "added jobs"
        when = _when(obs, "M")
        shown = [ex_row(_when(prev, "M"), f"{vals[prev]:,.0f}"),
                 ex_row(when, f"{vals[obs]:,.0f}", cited=True)]
        caveats = []
        f_now = await first_release(PAYEMS, obs)
        f_prev = None
        if f_now:
            rows_then = await _rows(PAYEMS, prev, prev, vintage=f_now[1])
            f_prev = rows_then[0][1] if rows_then else None
        if f_now and f_prev is not None:
            first_change = round(f_now[0] - f_prev)
            if first_change != change:
                caveats.append(f"First reported as {first_change:+,}k jobs "
                               f"({f_now[1]:%B %Y} archive); revised since to {change:+,}k.")
            if (first_change < 0) != (change < 0):
                caveats.append("The first report and the revised figure disagree on direction.")
        dp = _datapoint(PAYEMS, obs, change * 1000.0, f"{change:+,}",
                        make_excerpt(shown, units=PAYEMS.units,
                                     note=f"Change: {change:+,} thousand."),
                        metric="jobs_added", label="Change in total nonfarm payroll employment",
                        basis="change from the month before")
        head = (f"Claim says the U.S. {said} in {when}. BLS payrolls went from "
                f"{vals[prev]:,.0f}k to {vals[obs]:,.0f}k, a change of {change:+,}k jobs.")
        return _verdict(SUPPORTED if right else CONTRADICTED, head, said,
                        f"{change:+,}k jobs", [dp], caveats)
    return None


# ------------------------------------------------------------------ change since

_RISE_RE = re.compile(r"\b(risen|rose|rise|rising|up|increased|gone up|climbed|jumped|soared|grown)\b", re.I)
_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|percent\b)", re.I)


def _comparator(text: str) -> str:
    low = text.lower()
    if re.search(r"\b(more than|over|above|at least|upwards of|in excess of|nearly|almost)\b", low):
        return "less than" if re.search(r"\b(nearly|almost)\b", low) else "greater than"
    if re.search(r"\b(less than|under|below|at most|fewer than)\b", low):
        return "less than"
    return "equals"


async def _change_since(text: str) -> Optional[Verdict]:
    """"Prices have risen more than 25% since January 2021": the CPI-U index
    now against that month, which no agency headline states."""
    m = _SINCE_RE.search(text)
    if not m or _topic(text) not in ("prices", "inflation") or not _RISE_RE.search(text):
        return None
    pct = _PCT_RE.search(text)
    if not pct:
        return None
    if re.search(r"\b(a|per|each|every)\s+year\b|\bannual(ly)?\b|\byear[- ]over[- ]year\b", text, re.I):
        return None  # a yearly rate, not the total change
    base = date(int(m.group(2)), _month_of(m.group(1)) if m.group(1) else 1, 1)
    near = bool(re.search(r"\b(nearly|almost)\b", text, re.I))
    return await op_change_since("prices", base, float(pct.group(1)), _comparator(text), near)


async def op_change_since(topic: str, base: date, claimed: float, comp: str = "equals",
                          near: bool = False) -> Optional[Verdict]:
    """Engine: the percent change in a series from `base` to its latest value.
    "prices" is the CPI-U as BLS publishes it; "fred:<ID>" any monthly FRED series."""
    from evidence.sources import SourceUnavailable
    if topic == "prices":
        # The index BLS publishes and its CPI calculator uses: not seasonally
        # adjusted. FRED's adjusted copy only if BLS cannot be reached.
        series = CPI_NSA
        try:
            rows = await _bls_monthly("CUUR0000SA0", base.year, datetime.now(timezone.utc).year)
            rows = [r for r in rows if r[0] >= base]
        except SourceUnavailable:
            rows = []
        if len(rows) < 2 or rows[0][0] != base:
            series = CPI_SA
            rows = await _rows(CPI_SA, base)
        what, label = "prices", "Change in consumer prices (CPI-U)"
    elif topic.startswith("fred:"):
        series = fred_series(topic[5:])
        rows = await _rows(series, base)
        what, label = series.title, f"Change in {series.title}"
    else:
        return None
    if len(rows) < 2 or rows[0][0] != base:
        return None
    (d0, v0), (d1, v1) = rows[0], rows[-1]
    if v0 == 0:
        return None
    change = (v1 / v0 - 1) * 100
    if comp == "greater than":
        right = change > claimed
    elif comp == "less than":
        right = change < claimed and (not near or change >= claimed * 0.9)
    else:
        right = abs(change - claimed) <= max(0.5, abs(claimed) * 0.05)
    shown = [ex_row(_when(d0, "M"), f"{v0:,.3f}"), ex_row(_when(d1, "M"), f"{v1:,.3f}", cited=True)]
    dp = _datapoint(series, d1, change, f"{change:.1f}",
                    make_excerpt(shown, units=series.units,
                                 note=f"Change: {v1:,.3f} / {v0:,.3f} - 1 = {change:.1f}%."),
                    metric="price_change" if topic == "prices" else f"change:{series.sid}",
                    label=label, basis=f"{_when(d0, 'M')} to {_when(d1, 'M')}")
    dp = replace(dp, quantity=Quantity(magnitude=change, family=PERCENT, raw=f"{change:.1f}"))
    phrase = {"greater than": "more than ", "less than": "nearly " if near else "less than "}.get(comp, "")
    head = (f"Claim says {what} rose {phrase}{claimed:g}% since {_when(d0, 'M')}. "
            f"{'The CPI-U' if topic == 'prices' else series.sid} went from "
            f"{v0:,.3f} to {v1:,.3f} by {_when(d1, 'M')}: {'up' if change >= 0 else 'down'} "
            f"{abs(change):.1f}%.")
    outcome = SUPPORTED if right else CONTRADICTED
    if not right and comp == "equals" and abs(change - claimed) <= abs(claimed) * 0.15:
        outcome = MOSTLY_ACCURATE
    return _verdict(outcome, head, f"{phrase}{claimed:g}%", f"{change:.1f}%", [dp], comparator=comp)


# ------------------------------------------------------------------ records

_WORDS = {"a": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
          "eight": 8, "nine": 9, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30,
          "forty": 40, "fifty": 50}
_RECORD_RE = re.compile(
    r"\b(highest|lowest|biggest|largest|smallest|worst|best)\b[^.]*?\b(?:"
    r"in\s+(?:(?:more\s+than|over|nearly|almost)\s+)?(?P<n>\d+|a|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"fifteen|twenty|thirty|forty|fifty)\s+(?P<unit>decades?|years?)"
    r"|since\s+(?P<since>(?:19|20)\d\d))\b"
    r"|\b(?P<n2>\d+|two|three|four|five|ten|twenty|thirty|forty|fifty)[- ]year\s+(?P<hl>high|low)\b",
    re.I)


async def _record(text: str) -> Optional[Verdict]:
    """"The highest in a decade", "the lowest since 1969", "a 40-year high", for
    unemployment and inflation: the claim's month against every month in the
    window, current and as first published."""
    m = _RECORD_RE.search(text)
    topic = _topic(text)
    if not m or topic not in ("unemployment", "inflation"):
        return None
    word = (m.group(1) or m.group("hl") or "").lower()
    high = word in ("highest", "biggest", "largest", "high") or (
        word == "worst" and topic in ("unemployment", "inflation"))
    if word == "best":
        high = False
    obs, freq = _subject_period(_RECORD_RE.sub(" ", text))
    if freq == "Q":
        return None
    # Window start.
    if m.group("since"):
        since_year = int(m.group("since"))
        span = None
    else:
        raw = (m.group("n") or m.group("n2")).lower()
        n = int(raw) if raw.isdigit() else _WORDS[raw]
        if (m.group("unit") or "").lower().startswith("decade"):
            n *= 10
        span, since_year = n, None
    return await op_record(topic, obs, high, span, since_year)


async def op_record(topic: str, obs: Optional[date], high: bool, span: Optional[int],
                    since_year: Optional[int]) -> Optional[Verdict]:
    """Engine: was the value in month `obs` (latest when None) the highest (or
    lowest) in `span` years, or since `since_year`? "unemployment",
    "inflation" (CPI-U, 12-month change) or "fred:<ID>" (any monthly series)."""
    if topic == "unemployment":
        rows = await _rows(UNRATE)
        series, metric, fmt = UNRATE, "unemployment_rate", "{:.1f}"
    elif topic.startswith("fred:"):
        series = fred_series(topic[5:])
        rows = await _rows(series)
        metric, fmt = f"fred:{series.sid}", "{:,.3g}"
    elif topic == "inflation":
        now = datetime.now(timezone.utc).year
        first = (since_year if span is None else now - span - 1) - 1
        idx = await _bls_monthly("CUUR0000SA0", first, now)
        by = {d: v for d, v in idx}
        rows = [(d, round((v / by[date(d.year - 1, d.month, 1)] - 1) * 100, 1))
                for d, v in idx if date(d.year - 1, d.month, 1) in by]
        series, metric, fmt = replace(CPI_NSA, family=PERCENT, units="percent change from a year earlier"), \
            "inflation_rate", "{:.1f}"
    else:
        return None
    if not rows:
        return None
    unit = "%" if series.family == PERCENT else ""

    def fv(x: float) -> str:
        return fmt.format(x) + unit
    vals = dict(rows)
    if obs is None:
        obs = rows[-1][0]
    if obs not in vals:
        return None
    value = vals[obs]
    if span is not None:
        start = date(obs.year - span, obs.month, 1)
        window = [(d, v) for d, v in rows if start <= d < obs]
        span_text = f"the {span} years before"
        no_month = f"no month in the {span} years before"
    else:
        window = [(d, v) for d, v in rows if d.year > since_year and d < obs]
        span_text = f"since {since_year}"
        no_month = f"no month since {since_year}"
    if not window:
        return None
    beat = [(d, v) for d, v in window if (v >= value if high else v <= value)]
    right = not beat

    caveats = []
    first = None
    if topic == "unemployment" and not right:
        first = await first_release(UNRATE, obs)
        if first:
            fbeat = [(d, v) for d, v in window if (v >= first[0] if high else v <= first[0])]
            if not fbeat:
                caveats.append(f"True as first reported: BLS published {fv(first[0])} for "
                               f"{_when(obs, 'M')} ({first[1]:%B %-d, %Y} archive), since revised to "
                               f"{fv(value)}.")
                right = True
    best = (max if high else min)(window, key=lambda r: r[1])
    shown = [ex_row(_when(obs, "M"), fmt.format(value), cited=True),
             ex_row(f"{'Highest' if high else 'Lowest'}, {span_text}: {_when(best[0], 'M')}",
                    fmt.format(best[1]))]
    if first:
        shown.insert(1, ex_row(f"{_when(obs, 'M')}, first published", fmt.format(first[0])))
    dp = _datapoint(series, obs, value, fmt.format(value),
                    make_excerpt(shown, units=series.units), metric=metric,
                    label=("Unemployment rate" if topic == "unemployment"
                           else "Inflation (CPI-U, change from a year earlier)" if topic == "inflation"
                           else series.title))
    what = {"unemployment": "unemployment", "inflation": "inflation"}.get(topic, series.title)
    kind = "highest" if high else "lowest"
    window_text = f"in {span} years" if span else f"since {since_year}"
    if right and not caveats:
        head = (f"{what.title()} was {fv(value)} in {_when(obs, 'M')}; {no_month} "
                f"was as {'high' if high else 'low'} "
                f"({'highest' if high else 'lowest'}: {fv(best[1])} in {_when(best[0], 'M')}).")
    elif right:
        head = (f"{what.title()} was first published at {fv(first[0])} for {_when(obs, 'M')}, "
                f"the {kind} {window_text}; it is now {fv(value)}.")
    else:
        d, v = (max if high else min)(beat, key=lambda r: r[1])
        head = (f"Claim says {what} was the {kind} {window_text}. It was {fv(value)} in "
                f"{_when(obs, 'M')}, but it was {fv(v)} in {_when(d, 'M')}.")
    return _verdict(SUPPORTED if right else CONTRADICTED, head, f"{kind} {window_text}",
                    f"{fv(value)} ({_when(obs, 'M')})", [dp], caveats)


async def record_aside(text: str) -> Optional[Verdict]:
    """The record part of a claim that also states a figure ("fell to 3.4% in
    January 2023, the lowest since 1969"), checked on its own."""
    if not _RECORD_RE.search(text):
        return None
    try:
        return await _record(text)
    except Exception:  # noqa: BLE001 — an aside never breaks the main check
        logger.exception("Record aside failed for %r", text[:120])
        return None


# ------------------------------------------------------------------ first print

_FIRST_PRINT = {"unemployment_rate": UNRATE, "real_gdp_growth": GDP}


async def first_print_match(parsed, v: Verdict) -> Optional[Verdict]:
    """A near miss or a contradiction that matches what the agency first
    published. Claims quote the release they read; BLS and BEA revise later.
    Returns the claim judged against the first print, with the revision named."""
    spec = parsed.metric
    s = _FIRST_PRINT.get(spec.metric if spec else "")
    if s is None or not v.evidence or parsed.claimed_value is None:
        return None
    if parsed.geography is not None and parsed.geography.level != "nation":
        return None
    dp = v.evidence[0]
    obs_p = dp.observed
    if s.freq == "M" and obs_p.month and obs_p.year:
        obs = date(obs_p.year, obs_p.month, 1)
    elif s.freq == "Q" and obs_p.quarter and obs_p.year:
        obs = date(obs_p.year, 3 * obs_p.quarter - 2, 1)
    else:
        return None
    first = await first_release(s, obs)
    if not first:
        return None
    value, vd = first
    claimed = parsed.claimed_value.magnitude
    tol = parsed.claimed_value.tolerance() or 0.05
    if abs(value - dp.quantity.magnitude) < 1e-9 or abs(value - claimed) > tol + 1e-9:
        return None
    note = (f"{dp.label} for {_when(obs, s.freq)} was first published as {value:g}% "
            f"({vd:%B %-d, %Y} archive), which the claim matches; it has since been revised to "
            f"{dp.quantity.magnitude:g}%.")
    return replace(v, verdict=SUPPORTED, confidence=MEDIUM,
                   headline=f"{v.headline.split(': close')[0].rstrip('.')}. {note}")


# ------------------------------------------------------------------ fixed facts

# Decennial census resident population of the United States, as counted.
_DECENNIAL = {2020: 331_449_281, 2010: 308_745_538, 2000: 281_421_906}
_DECENNIAL_RE = re.compile(r"\b(2020|2010|2000)\s+(?:decennial\s+)?census\b|\bcensus\s+(?:of|in)\s+(2020|2010|2000)\b",
                           re.I)


def _census_count(text: str, parsed) -> Optional[Verdict]:
    m = _DECENNIAL_RE.search(text)
    if not m or parsed.claimed_value is None or parsed.claimed_value.family not in (PERSONS, "unknown"):
        return None
    if parsed.geography is not None and parsed.geography.level != "nation":
        return None
    if not re.search(r"\b(counted|population|people|residents|americans|enumerated)\b", text, re.I):
        return None
    year = int(m.group(1) or m.group(2))
    count = _DECENNIAL[year]
    from claims.verdict import assess
    dp = Datapoint(
        quantity=Quantity(magnitude=float(count), family=PERSONS, raw=f"{count:,}"),
        metric="population", label="Resident population of the United States",
        observed=Period(year=year, kind=POINT_IN_TIME, basis=f"{year} Census, April 1, {year}",
                        record_date=f"{year}-04-01"),
        geography=Geography.national(), source="CENSUS",
        source_url=f"https://www.census.gov/programs-surveys/decennial-census/decade.{year}.html",
        raw_value=str(count),
        extra={"excerpt": make_excerpt([ex_row(f"{year} Census, resident population", f"{count:,}",
                                               cited=True)], units="persons")},
    )
    pc = replace(parsed, year=year, month=None)
    return assess(pc, [dp])


async def _debt_held_by_public(text: str, parsed) -> Optional[Verdict]:
    """"Debt held by the public" is a part of the debt ($32.7T in October 2026),
    not the $40T total that the headline figure is."""
    if not re.search(r"\bheld by the public\b|\bpublicly held\b|\bpublic(?:ly)?[- ]held\b", text, re.I):
        return None
    if parsed.claimed_value is None or parsed.claimed_value.family not in (USD, "unknown"):
        return None
    from config import TREASURY_FISCAL_BASE_URL
    from claims.verdict import assess
    params = {"fields": "record_date,debt_held_public_amt,intragov_hold_amt,tot_pub_debt_out_amt",
              "sort": "-record_date", "page[size]": "1", "format": "json"}
    if parsed.year:
        params["filter"] = f"record_date:lte:{parsed.year}-12-31"

    async def load():
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(f"{TREASURY_FISCAL_BASE_URL}/v2/accounting/od/debt_to_penny", params=params)
            r.raise_for_status()
            return r.json().get("data") or []
    try:
        rows = await cached("derived_debt_public", str(parsed.year), 6 * 3600, load)
    except httpx.HTTPError as e:
        logger.warning("Treasury debt held by the public failed: %s", e)
        return None
    if not rows:
        return None
    r0 = rows[0]
    public, total = float(r0["debt_held_public_amt"]), float(r0["tot_pub_debt_out_amt"])
    d = r0["record_date"]
    dp = Datapoint(
        quantity=Quantity(magnitude=public, family=USD, raw=r0["debt_held_public_amt"]),
        metric="national_debt", label="Debt held by the public",
        observed=Period(year=int(d[:4]), kind=POINT_IN_TIME, basis="Debt to the Penny", record_date=d),
        geography=Geography.national(), source="TREASURY",
        source_url="https://fiscaldata.treasury.gov/datasets/debt-to-the-penny/",
        raw_value=r0["debt_held_public_amt"],
        extra={"excerpt": make_excerpt([
            ex_row("Debt held by the public", f"{public:,.2f}", cited=True),
            ex_row("Intragovernmental holdings", f"{float(r0['intragov_hold_amt']):,.2f}"),
            ex_row("Total public debt outstanding", f"{total:,.2f}")], units="dollars",
            note=f"Record date {d}.")},
    )
    pc = replace(parsed, year=None, month=None)
    v = assess(pc, [dp])
    claimed = parsed.claimed_value.magnitude
    if v.verdict != SUPPORTED and abs(claimed - total) <= max(abs(total) * 0.03, 5e11):
        note = (f"{humanize(claimed, USD)} is the total debt ({humanize(total, USD)}), which includes "
                f"debt the government owes its own trust funds; debt held by the public is "
                f"{humanize(public, USD)}.")
        v = replace(v, verdict=MISLEADING, headline=f"{v.headline} {note}", caveats=v.caveats + [note])
    return v


# ------------------------------------------------------------------ took office

_TOOK_OFFICE = {"biden": (2021, 1, "January 20, 2021"), "obama": (2009, 1, "January 20, 2009")}
_OFFICE_RE = re.compile(
    r"\b(Biden|Obama|Trump)\b(?=[^.]*\b(inherited|took office|came (?:in|into office)|"
    r"was (?:sworn in|inaugurated)|first day|day one|entered office)\b)", re.I)


def took_office(text: str) -> Tuple[str, str]:
    """"Biden inherited 9% inflation when he took office" names a month without
    saying it: January 2021. Returns (the text with the month written in, a
    caveat), or the text unchanged when no month can be pinned down (Trump took
    office twice; the claim must say which)."""
    if _MONTH_YEAR_RE.search(text) or re.search(r"\b(19|20)\d\d\b", text):
        return text, ""
    m = _OFFICE_RE.search(text)
    if not m:
        return text, ""
    who = m.group(1).lower()
    if who not in _TOOK_OFFICE:
        return text, ""
    y, mo, day = _TOOK_OFFICE[who]
    note = (f"{m.group(1).title()} took office on {day}; this checks {_MONTHS[mo - 1]} {y}, "
            f"the month he took office.")
    since = re.compile(rf"\bsince\s+(?:President\s+)?{m.group(1)}\s+(?:took office|came into office|"
                       rf"was (?:sworn in|inaugurated)|entered office)\b", re.I)
    if since.search(text):
        return since.sub(f"since {_MONTHS[mo - 1]} {y}", text), note
    out = re.sub(r"\s*\bwhen (?:he|she) (?:took office|came into office|was sworn in)\b", "", text,
                 flags=re.I).rstrip(" .") + f" in {_MONTHS[mo - 1]} {y}."
    return out, note


async def _national_income(parsed) -> Optional[Verdict]:
    """U.S. median household income for a year, against the CPS figure Census
    headlines. Only a match is returned; otherwise the ACS check runs."""
    spec = parsed.metric
    if spec is None or spec.metric != "median_household_income" or parsed.claimed_value is None:
        return None
    if parsed.geography is not None and parsed.geography.level != "nation":
        return None
    if not parsed.year or parsed.month or parsed.is_trend_claim:
        return None
    from claims.verdict import assess
    dp = await cps_median_income(parsed.year)
    if dp is None:
        return None
    v = assess(parsed, [dp])
    return v if v.verdict in (SUPPORTED, MOSTLY_ACCURATE) else None


# ------------------------------------------------------------------ entry point

async def check(text: str, parsed) -> Optional[Verdict]:
    """A computed answer for the claim, or None to run the normal pipeline."""
    try:
        for step in (_direction, _change_since):
            v = await step(text)
            if v is not None:
                return v
        if _RECORD_RE.search(text) and parsed.claimed_value is None:
            v = await _record(text)
            if v is not None:
                return v
        v = _census_count(text, parsed)
        if v is not None:
            return v
        v = await _national_income(parsed)
        if v is not None:
            return v
        return await _debt_held_by_public(text, parsed)
    except Exception:  # noqa: BLE001 — never block the normal pipeline
        logger.exception("Computed check failed for %r", text[:120])
        return None


# ------------------------------------------------------------------ CPS income

async def cps_median_income(year: int) -> Optional[Datapoint]:
    """Census's headline median household income (CPS ASEC, current dollars),
    the figure its September report and the news quote. The wired series is
    the ACS estimate, a different survey that runs several percent lower."""
    s = Series("MEHOINUSA646N", "A", USD, "Median household income (CPS ASEC)",
               ("U.S. Census Bureau, Current Population Survey", "Census"), "current dollars")
    rows = await _rows(s, date(year, 1, 1), date(year, 12, 31))
    if not rows:
        return None
    _, v = rows[0]
    return Datapoint(
        quantity=Quantity(magnitude=v, family=USD, raw=f"{v:.0f}"),
        metric="median_household_income", label="Median household income (CPS ASEC)",
        observed=Period(year=year, basis="Current Population Survey, Annual Social and Economic Supplement"),
        geography=Geography.national(), source="FRED",
        source_url=f"https://fred.stlouisfed.org/series/{s.sid}", series_id=s.sid, raw_value=f"{v:.0f}",
        extra={"original_source": s.publisher[0], "publisher_short": "Census", "units": s.units,
               "excerpt": make_excerpt([ex_row(f"{year}", f"{v:,.0f}", cited=True)], units=s.units)},
    )


async def latest_gdp_growth() -> List[Datapoint]:
    """The latest quarter's real GDP growth, for "GDP growth" asked with no
    period: the current figure, not last year's annual rate."""
    today = datetime.now(timezone.utc).date()
    rows = await _rows(GDP, date(today.year - 2, 1, 1))
    if not rows:
        return []
    d, v = rows[-1]
    shown = [ex_row(_when(d, "Q"), f"{v:.1f}", cited=True)]
    return [_datapoint(GDP, d, v, f"{v:.1f}", make_excerpt(shown, units=GDP.units),
                       metric="real_gdp_growth", label="Real GDP, percent change",
                       basis="latest quarter, seasonally adjusted annual rate")]


async def gdp_quarter(year: int, quarter: int) -> List[Datapoint]:
    """Real GDP growth for one quarter, from FRED's copy of BEA's series. Used
    when no BEA key is set."""
    obs = date(year, 3 * quarter - 2, 1)
    rows = await _rows(GDP, obs, obs)
    if not rows:
        return []
    _, v = rows[0]
    when = _when(obs, "Q")
    return [_datapoint(GDP, obs, v, f"{v:.1f}",
                       make_excerpt([ex_row(when, f"{v:.1f}", cited=True)], units=GDP.units),
                       metric="real_gdp_growth", label="Real GDP, percent change",
                       basis="seasonally adjusted annual rate")]
