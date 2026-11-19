"""Federal contracts and grants, from USAspending.gov (Treasury's DATA Act site).

One award at a time. Agency budget totals are api/usaspending.py.

Claims about a contract or a grant ("ICE signed a new order with CSI worth up
to $1.28bn", "the department awarded $40m to ...") name a record, not a
statistic. Web search finds such a record late or not at all, and confuses it
with last year's: the ICE order above was held against the previous year's
order until this source existed. USAspending publishes every federal award
with its parties, the day it was signed, its period and its money, through a
public API with no key.

Two steps, as for the other datasets:

  search(words, recipient, agency, signed_from, signed_to, kind)
      awards that match, largest first: id, recipient, agency, dates, amount
  fetch(award_id, measure)
      one figure of one award:
        ceiling     the most it can reach with every option ("worth up to")
        obligated   money committed so far
        outlays     money actually paid out so far

A ceiling, money obligated and money paid are different figures; the measure
is named on every one, and the finder is told to match the claim's.
"""
from datetime import date
from typing import Any, Dict, List, Optional

import httpx

import config
from config import logger
from evidence.cache import cached
from evidence.datapoint import POINT_IN_TIME, Datapoint, Geography, Period
from evidence.excerpt import make as make_excerpt, row as ex_row
from evidence.sources import SourceUnavailable
from evidence.units import USD, Quantity

_BASE = f"{config.USASPENDING_BASE_URL.rstrip('/')}/api/v2"
_HOUR = 3600
KINDS = {"contracts": ["A", "B", "C", "D"], "grants": ["02", "03", "04", "05"]}
MEASURES = {
    "ceiling": ("base_and_all_options", "potential total value, with all options (the most it can reach)"),
    "obligated": ("total_obligation", "money obligated so far"),
    "outlays": ("total_outlay", "money paid out so far"),
}


def available() -> bool:
    return True


async def _call(method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await (c.post(f"{_BASE}/{path}", json=body) if method == "POST" else c.get(f"{_BASE}/{path}"))
    except httpx.HTTPError as e:
        raise SourceUnavailable("USAspending", f"request failed: {type(e).__name__}") from e
    if r.status_code in (400, 404, 422):
        return None
    if r.status_code >= 400:
        raise SourceUnavailable("USAspending", f"HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError:
        return None


def _day(s: Any) -> Optional[str]:
    try:
        return date.fromisoformat(str(s)[:10]).isoformat()
    except ValueError:
        return None


async def search(words: str = "", recipient: str = "", agency: str = "", signed_from: str = "",
                 signed_to: str = "", kind: str = "contracts", limit: int = 8) -> List[Dict[str, Any]]:
    """Awards that match, largest first. Every filter is optional but at least
    one of words and recipient is needed."""
    kw = [w for w in (words or "").split() if len(w) >= 3][:6]
    filters: Dict[str, Any] = {"award_type_codes": KINDS.get(kind, KINDS["contracts"])}
    if kw:
        filters["keywords"] = kw
    if recipient.strip():
        filters["recipient_search_text"] = [recipient.strip()[:80]]
    if not kw and "recipient_search_text" not in filters:
        return []
    if agency.strip():
        filters["agencies"] = [{"type": "awarding", "tier": "toptier", "name": agency.strip()[:120]}]
    start, end = _day(signed_from), _day(signed_to)
    if start or end:
        filters["time_period"] = [{"start_date": start or "2007-10-01", "end_date": end or date.today().isoformat(),
                                   "date_type": "date_signed"}]
    body = {"filters": filters, "limit": max(1, min(limit, 10)), "sort": "Award Amount", "order": "desc",
            "fields": ["Award ID", "Recipient Name", "Award Amount", "Start Date", "End Date",
                       "Awarding Agency", "Description", "generated_internal_id"]}

    async def load():
        data = await _call("POST", "search/spending_by_award/", body)
        return (data or {}).get("results") or []
    import json
    rows = await cached("usaspending_search", json.dumps(body, sort_keys=True), 6 * _HOUR, load) or []
    out = []
    for r in rows:
        if not r.get("generated_internal_id"):
            continue
        out.append({"award": r["generated_internal_id"], "id": r.get("Award ID"),
                    "recipient": r.get("Recipient Name"), "agency": r.get("Awarding Agency"),
                    "obligated": r.get("Award Amount"), "start": r.get("Start Date"), "end": r.get("End Date"),
                    "about": str(r.get("Description") or "")[:140]})
    return out


async def fetch(award: str, measure: str = "ceiling") -> Optional[Datapoint]:
    """One figure of one award, or None if the award has no such figure."""
    if measure not in MEASURES or not award or "/" in award:
        return None
    field, what = MEASURES[measure]

    async def load():
        return await _call("GET", f"awards/{award}/")
    d = await cached("usaspending_award", award, 6 * _HOUR, load)
    if not isinstance(d, dict):
        return None
    value = d.get(field)
    if value in (None, ""):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    pid = d.get("piid") or d.get("fain") or d.get("uri") or award
    recipient = ((d.get("recipient") or {}).get("recipient_name") or "").strip()
    agency = (((d.get("awarding_agency") or {}).get("toptier_agency") or {}).get("name") or "").strip()
    pop = d.get("period_of_performance") or {}
    signed = _day(d.get("date_signed"))
    start, end = _day(pop.get("start_date")), _day(pop.get("end_date"))
    kind = "contract" if str(award).startswith("CONT_") else "award"
    label = f"{agency} {kind} {pid} with {recipient}: {what}".strip()
    period = Period(year=int(signed[:4]) if signed else None, kind=POINT_IN_TIME,
                    basis=f"signed {signed}" + (f"; runs {start} to {end}" if start and end else ""),
                    record_date=signed)
    url = f"https://www.usaspending.gov/award/{award}"
    rows = [ex_row("Potential total value (all options)", d.get("base_and_all_options"), cited=measure == "ceiling"),
            ex_row("Obligated so far", d.get("total_obligation"), cited=measure == "obligated"),
            ex_row("Paid out so far", d.get("total_outlay"), cited=measure == "outlays")]
    return Datapoint(
        quantity=Quantity(magnitude=value, family=USD, raw=str(value)),
        metric=f"usaspending:{measure}", label=label, observed=period, geography=Geography.national(),
        source="USASPENDING", source_url=url, series_id=str(pid), as_of=signed, raw_value=str(value),
        extra={"titled_place": "", "measure": measure, "signed": signed, "start": start, "end": end,
               "recipient": recipient, "agency": agency,
               "excerpt": make_excerpt(rows, units="dollars")},
    )


def log_unavailable(e: Exception) -> None:
    logger.warning("USAspending unavailable: %s", e)
