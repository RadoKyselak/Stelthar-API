"""Treasury Fiscal Data — national debt, with the period read from the record.

The previous adapter filtered on `record_fiscal_year:eq:{year}` for a claim that
said a plain calendar year, sorted descending and took row[0]. US fiscal years
end September 30, so "the national debt in 2023" was answered with the
2023-09-30 figure — excluding October to December entirely. The debt crossed
$34 trillion on 2023-12-29, so a true claim about 2023 was contradicted by a
figure roughly $0.9 trillion low. And because the adapter then wrote
`record_fiscal_year` into the field the temporal guard reads, the guard saw
2023 == 2023 and stayed silent.

Here the calendar window is the default, fiscal filtering happens only when the
claim actually said "fiscal year", and the period returned describes the record
that came back rather than the one that was requested.
"""
from typing import Any, Dict, List, Optional

import httpx

from config import TREASURY_FISCAL_BASE_URL, logger
from config.constants import API_TIMEOUTS, RATE_LIMIT_QUOTAS
from evidence.cache import TTL_IMMUTABLE, TTL_VOLATILE, cached
from evidence.datapoint import (
    FISCAL_YEAR, POINT_IN_TIME, Datapoint, Geography, Period,
)
from evidence.units import USD, Quantity, parse_number
from utils.rate_limiter import get_quota_limiter
from utils.urls import public_url

_limiter = get_quota_limiter("TREASURY", *RATE_LIMIT_QUOTAS.TREASURY)

_ENDPOINT = "/v2/accounting/od/debt_to_penny"
_VALUE_FIELD = "tot_pub_debt_out_amt"
_LABEL = "Total public debt outstanding"


def _row_to_datapoint(row: Dict[str, Any], url: str, want_fiscal: bool) -> Optional[Datapoint]:
    value = parse_number(row.get(_VALUE_FIELD))
    if value is None:
        return None

    record_date = str(row.get("record_date") or "").strip()
    fiscal_year = row.get("record_fiscal_year")

    # The period comes from the record. A daily debt figure is a point in time,
    # not an annual average — saying otherwise would let it be compared against
    # a yearly aggregate as though they measured the same thing.
    if want_fiscal and fiscal_year:
        period = Period(
            year=int(fiscal_year), kind=FISCAL_YEAR,
            basis=f"fiscal year end, as of {record_date}" if record_date else "fiscal year end",
            record_date=record_date or None,
        )
    else:
        period = Period(
            year=int(record_date[:4]) if record_date[:4].isdigit() else None,
            kind=POINT_IN_TIME,
            basis=f"daily figure, as of {record_date}" if record_date else "daily figure",
            record_date=record_date or None,
        )

    return Datapoint(
        quantity=Quantity(magnitude=value, family=USD, raw=str(row.get(_VALUE_FIELD))),
        metric="national_debt",
        label=_LABEL,
        observed=period,
        geography=Geography.national(),
        source="TREASURY",
        source_url=url,
        series_id="debt_to_penny",
        as_of=record_date or None,
        raw_value=str(row.get(_VALUE_FIELD)),
    )


async def fetch_national_debt(
    year: Optional[int] = None,
    year_basis: str = "calendar",
) -> List[Datapoint]:
    """Fetch the debt for a year, or the latest figure when year is None.

    For a calendar year this returns the LAST record of that year, which is what
    "the debt in 2023" means to a reader. For a fiscal year it returns the
    fiscal-year-end record and labels it as such.
    """
    await _limiter.acquire()

    query: Dict[str, str] = {
        "fields": f"record_date,record_fiscal_year,{_VALUE_FIELD}",
        "sort": "-record_date",
        "page[size]": "1",
        "format": "json",
    }
    want_fiscal = year is not None and year_basis == "fiscal"
    metric_key = "debt"

    if year is not None:
        if want_fiscal:
            query["filter"] = f"record_fiscal_year:eq:{year}"
        else:
            # Calendar window. Descending sort + size 1 gives the year's final
            # record, which is the figure a reader means by "in <year>".
            query["filter"] = f"record_date:gte:{year}-01-01,record_date:lte:{year}-12-31"

    url = f"{TREASURY_FISCAL_BASE_URL}{_ENDPOINT}"

    async def _load():
        async with httpx.AsyncClient(timeout=API_TIMEOUTS.TREASURY) as client:
            r = await client.get(url, params=query)
            r.raise_for_status()
            return {"payload": r.json(), "url": public_url(r.url)}

    # A closed year's year-end figure is final; the latest figure moves daily.
    from datetime import datetime as _dt, timezone as _tz
    ttl = TTL_IMMUTABLE if (year is not None and year < _dt.now(_tz.utc).year) else TTL_VOLATILE
    try:
        blob = await cached("treasury", f"{metric_key}:{year}:{year_basis}", ttl, _load)
    except httpx.HTTPError as e:
        logger.warning("Treasury fetch failed: %s", e)
        return []
    if not blob:
        return []
    payload, request_url = blob["payload"], blob["url"]

    rows = payload.get("data", []) if isinstance(payload, dict) else []
    if not rows:
        logger.info("Treasury returned no rows for year=%s basis=%s", year, year_basis)
        return []

    dp = _row_to_datapoint(rows[0], request_url, want_fiscal)
    return [dp] if dp else []


async def fetch_debt_series(start_year: int, end_year: int) -> List[Datapoint]:
    """Year-end debt for each year in a range — the input to trend analysis.

    A single datapoint cannot answer "the debt doubled under X". Fetching the
    series is what makes a trend claim checkable instead of unanswerable, and
    it is the primitive the framing checks are built on.
    """
    out: List[Datapoint] = []
    for y in range(start_year, end_year + 1):
        out.extend(await fetch_national_debt(year=y))
    return out
