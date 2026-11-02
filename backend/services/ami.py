"""Claims about Area Median Income and HUD income limits, checked against
HUD's own tables.

  "80% of AMI for a family of four in Austin is $95,000"
  "A family of four earning $60,000 is very low income in Denver"  (not yet: eligibility)
  "The area median income in Travis County is $126,000"

What is read:
  AMI (100%)      HUD's median family income for the area: a four-person figure
  30 / 50 / 80%   the extremely low, very low and low income limits, as published
  other %         HUD's method: the very low (50%) limit scaled, so 60% = 1.2 x 50%

Household size defaults to four, the size HUD's median is set for, and the
answer says so.
"""
import re
from dataclasses import replace
from datetime import datetime, timezone
from typing import Optional, Tuple

from claims.verdict import Verdict, assess
from config import logger
from evidence.datapoint import ANNUAL, Datapoint, Geography, Period
from evidence.excerpt import make as make_excerpt, row as ex_row
from evidence.sources import SourceUnavailable
from evidence.sources import hud
from evidence.units import USD, Quantity

_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
        "single": 1, "individual": 1, "couple": 2}
_SIZE_RE = re.compile(
    r"\b(?:family|household)\s+of\s+(one|two|three|four|five|six|seven|eight|[1-8])\b"
    r"|\b(one|two|three|four|five|six|seven|eight|[1-8])[- ](?:person|people|member)\b"
    r"|\b(single(?:[- ]person)?|individual|couple)\b", re.I)
_PCT_RE = re.compile(r"\b(\d{2,3})\s*(?:%|percent)\s*(?:of\s+)?(?:the\s+)?(?:area median income|AMI|"
                     r"median family income|MFI)\b", re.I)
_TIER_RE = re.compile(r"\b(extremely|very)?[- ]?low[- ]income\b", re.I)
_AMI_RE = re.compile(r"\barea median income\b|\bAMI\b|\bmedian family income\b|\bincome limits?\b", re.I)
_DOLLARS_RE = re.compile(r"\$\s*(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(k|thousand)?\b", re.I)


def read(text: str) -> Optional[Tuple[int, int, bool]]:
    """(percent of AMI, household size, size was stated) or None."""
    if not _AMI_RE.search(text) and not _TIER_RE.search(text):
        return None
    m = _PCT_RE.search(text)
    if m:
        pct = int(m.group(1))
    else:
        t = _TIER_RE.search(text)
        if t and _AMI_RE.search(text) is None and not _DOLLARS_RE.search(text):
            return None  # "low-income families" in passing, with no figure, is not a HUD claim
        pct = {"extremely": 30, "very": 50}.get((t.group(1) or "").lower(), 80) if t else 100
    if not 10 <= pct <= 200:
        return None
    s = _SIZE_RE.search(text)
    if s:
        word = next(g for g in s.groups() if g).lower().split("-")[0].split()[0]
        size = int(word) if word.isdigit() else _NUM.get(word, 4)
        return pct, size, True
    return pct, 4, False


_CITY_ST_RE = re.compile(r"\b((?:[A-Z][a-z.]+\s){0,2}[A-Z][a-z.]+),?\s+([A-Z]{2})\b")


def _city_state(text: str) -> Optional[Geography]:
    """"Austin TX" in a search box, with no comma."""
    from claims.geography import USPS
    for m in _CITY_ST_RE.finditer(text):
        if m.group(2) in USPS and m.group(1).split()[0].upper() not in ("AMI", "MFI"):
            return Geography(name=f"{m.group(1)}, {m.group(2)}", level="place")
    return None


def _claimed(text: str) -> Optional[Tuple[float, Optional[float]]]:
    """(dollars, the precision they were written to) or None."""
    from evidence.units import written_precision
    m = _DOLLARS_RE.search(text)
    if not m:
        return None
    scale = 1000.0 if m.group(2) else 1.0
    return float(m.group(1).replace(",", "")) * scale, written_precision(m.group(1), scale)


def _figure(table: dict, pct: int, size: int) -> Tuple[float, str]:
    """(dollars, how it was read)."""
    key = str(size)
    if pct in (30, 50, 80) and key in table.get(f"l{pct}", {}):
        name = {30: "extremely low", 50: "very low", 80: "low"}[pct]
        return table[f"l{pct}"][key], f"HUD's published {name} income limit ({pct}%)"
    if pct == 100 and size == 4:
        return table["median_income"], "HUD's area median family income (four-person)"
    base = table.get("l50", {}).get(key)
    if base is None:
        raise KeyError(key)
    return round(base * pct / 50), (f"{pct}% computed the way HUD does, from the very low (50%) "
                                    f"limit: {pct}/50 x ${base:,.0f}")


async def check(text: str, parsed) -> Optional[Verdict]:
    """The claim against HUD's table for its area and year, or None."""
    claimed = _claimed(text)
    if claimed is None:
        return None
    dp = await figure(text, parsed)
    if dp is None:
        return None
    claimed, precision = claimed
    Q = Quantity
    pc = replace(parsed, metric=None, year=dp.observed.year, month=None, geography=None,
                 claimed_value=Q(magnitude=claimed, family=USD, raw=f"${claimed:,.0f}",
                                 approximate=bool(re.search(r"\b(about|around|roughly|nearly|almost)\b", text, re.I)),
                                 precision=precision),
                 all_values=[], all_years=[dp.observed.year])
    return assess(pc, [dp])


async def figure(text: str, parsed) -> Optional[Datapoint]:
    """HUD's figure for the AMI or income limit the text names, or None."""
    if not hud.available():
        return None
    got = read(text)
    geo = parsed.geography or _city_state(text)
    if got is None or geo is None or not geo.name or geo.level in ("nation", "state"):
        return None
    pct, size, stated = got
    try:
        area = await hud.find_area(geo.name)
        if area is None:
            return None
        entity, area_name = area
        now = datetime.now(timezone.utc).year
        table = None
        for y in ([parsed.year] if parsed.year else [now, now - 1]):
            table = await hud.limits(entity, y)
            if table:
                break
        if not table:
            return None
        value, how = _figure(table, pct, size)
    except (SourceUnavailable, KeyError) as e:
        logger.warning("HUD check skipped for %r: %s", text[:80], e)
        return None
    fy = table["year"]
    label = (f"{pct}% of area median income, {size}-person household" if pct != 100 or size != 4
             else "Area median family income")
    shown = [ex_row(f"Median family income (AMI), FY{fy}", f"{table['median_income']:,.0f}",
                    cited=(pct == 100 and size == 4))]
    for p in (30, 50, 80):
        v = table.get(f"l{p}", {}).get(str(size))
        if v is not None:
            shown.append(ex_row(f"{p}% limit, {size}-person, FY{fy}", f"{v:,.0f}",
                                cited=(p == pct)))
    if pct not in (30, 50, 80, 100) or (pct == 100 and size != 4):
        shown.append(ex_row(f"{pct}%, {size}-person (computed)", f"{value:,.0f}", cited=True))
    dp = Datapoint(
        quantity=Quantity(magnitude=value, family=USD, raw=f"{value:.0f}"),
        metric="hud_income_limit", label=label,
        observed=Period(year=fy, kind=ANNUAL, basis=f"HUD FY{fy} income limits"),
        geography=Geography(name=table["area_name"] or area_name, level="metro"),
        source="HUD", source_url=f"https://www.huduser.gov/portal/datasets/il.html#year{fy}",
        series_id=entity, raw_value=f"{value:.0f}",
        extra={"publisher_short": "HUD", "excerpt": make_excerpt(shown, units="dollars per year"),
               "caveat": how + "." + ("" if stated else
                                      " The claim names no household size; HUD's figures are "
                                      "set for a family of four, so four is used.")},
    )
    return dp
