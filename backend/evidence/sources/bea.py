"""BEA NIPA — GDP, growth and personal income, period read from TimePeriod.

The legacy adapter matched rows on `LineCode`/`SeriesCode`. The NIPA GetData
response carries neither for the line number — it carries `LineNumber` — so a
request for line "1" compared "1" against a series code like "A191RC" and
matched nothing. Every GDP claim fell through to the planner.

Here a row must satisfy all of:
  * `LineNumber` equals the registry's line,
  * `LineDescription` contains the registry's expected text (a guard against a
    table being renumbered underneath us: a wrong line fails closed rather
    than returning a different aggregate under the right label),
  * `TimePeriod` is the year asked for (read from the row, never assumed).

Values are scaled by the row's own `UNIT_MULT` exponent, so "27,720,709" with
UNIT_MULT 6 becomes $27.72 trillion in base dollars before any comparison.
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from config import BEA_API_KEY, logger
from config.constants import API_TIMEOUTS, RATE_LIMIT_QUOTAS
from evidence.cache import TTL_IMMUTABLE, TTL_VOLATILE, cached
from evidence.datapoint import ANNUAL, Datapoint, Geography, Period
from evidence.sources import SourceUnavailable
from evidence.units import PERCENT, Quantity, parse_number, scale_from_exponent
from utils.rate_limiter import get_quota_limiter
from utils.urls import public_url

_limiter = get_quota_limiter("BEA", *RATE_LIMIT_QUOTAS.BEA)
_ENDPOINT = "https://apps.bea.gov/api/data"

# Error codes BEA uses for credential problems, as opposed to "no data".
_AUTH_ERROR_HINTS = ("userid", "user id", "not registered", "invalid api", "inactive")


# Reader-facing pages for each table. The API URL carries the key, so it is
# never the citation; it is kept in `extra["api_url"]` (redacted) for audit.
_TABLE_PAGES = {
    "T10105": "https://www.bea.gov/data/gdp/gross-domestic-product",
    "T10101": "https://www.bea.gov/data/gdp/gross-domestic-product",
    "T20100": "https://www.bea.gov/data/income-saving/personal-income",
}


def table_permalink(table: str) -> str:
    return _TABLE_PAGES.get(table, "https://apps.bea.gov/iTable/?reqid=19&step=2&isuri=1&categories=survey")


def _bea_error(payload: Dict[str, Any]) -> Optional[str]:
    api = payload.get("BEAAPI") or {}
    err = api.get("Error") or (api.get("Results") or {}).get("Error")
    if not err:
        return None
    if isinstance(err, list):
        err = err[0] if err else {}
    if isinstance(err, dict):
        return str(err.get("APIErrorDescription") or err.get("ErrorDetail") or err)
    return str(err)


async def _fetch_table(table: str, years: List[int]) -> Optional[Dict[str, Any]]:
    if not BEA_API_KEY:
        raise SourceUnavailable("BEA", "BEA_API_KEY not configured")

    year_param = ",".join(str(y) for y in sorted(set(years)))
    params = {
        "UserID": BEA_API_KEY,
        "method": "GetData",
        "DataSetName": "NIPA",
        "TableName": table,
        "Frequency": "A",
        "Year": year_param,
        "ResultFormat": "json",
    }

    async def _load():
        await _limiter.acquire()
        try:
            async with httpx.AsyncClient(timeout=API_TIMEOUTS.BEA) as client:
                r = await client.get(_ENDPOINT, params=params)
                r.raise_for_status()
                payload = r.json()
        except httpx.HTTPError as e:
            raise SourceUnavailable("BEA", f"request failed: {e}") from e
        except ValueError as e:
            raise SourceUnavailable("BEA", "non-JSON response") from e

        err = _bea_error(payload)
        if err:
            if any(h in err.lower() for h in _AUTH_ERROR_HINTS):
                raise SourceUnavailable("BEA", err[:160])
            # Typically "no data for the parameters requested" — an absence.
            logger.info("BEA %s %s: %s", table, year_param, err[:200])
            return None
        rows = ((payload.get("BEAAPI") or {}).get("Results") or {}).get("Data") or []
        if not rows:
            return None
        return {"rows": rows, "url": public_url(r.url)}

    current = datetime.now(timezone.utc).year
    # Recent years are revised every July and September; older ones rarely.
    ttl = TTL_VOLATILE * 24 if max(years) >= current - 2 else TTL_IMMUTABLE
    return await cached("bea", f"{table}:{year_param}", ttl, _load)


def _row_to_datapoint(spec, row: Dict[str, Any], api_url: str) -> Optional[Datapoint]:
    period = str(row.get("TimePeriod") or "").strip()
    if not (len(period) == 4 and period.isdigit()):
        return None  # annual request; anything else is not what we asked for
    raw = row.get("DataValue")
    value = parse_number(raw)
    if value is None:
        return None
    if spec.family != PERCENT:
        value *= scale_from_exponent(row.get("UNIT_MULT"))

    desc = str(row.get("LineDescription") or "").strip()
    return Datapoint(
        quantity=Quantity(magnitude=value, family=spec.family, raw=str(raw)),
        metric=spec.metric,
        label=spec.label,
        observed=Period(year=int(period), kind=ANNUAL,
                        basis="annual" if spec.family != PERCENT else "annual, vs prior year"),
        geography=Geography.national(),
        source="BEA",
        source_url=table_permalink(spec.bea_table),
        series_id=str(row.get("SeriesCode") or f"{spec.bea_table} line {spec.bea_line}"),
        as_of=None,
        raw_value=str(raw),
        extra={"table": spec.bea_table, "line": spec.bea_line,
               "line_description": desc, "api_url": api_url,
               "metric_name": row.get("METRIC_NAME")},
    )


def _matches_line(spec, row: Dict[str, Any]) -> bool:
    if str(row.get("LineNumber") or "").strip() != str(spec.bea_line):
        return False
    expect = (spec.bea_expect or "").lower()
    return not expect or expect in str(row.get("LineDescription") or "").lower()


async def fetch(spec, year: Optional[int]) -> List[Datapoint]:
    """Annual figure for `year`, or the latest published when year is None."""
    current = datetime.now(timezone.utc).year
    years = [year] if year is not None else [current - k for k in range(0, 4)]
    blob = await _fetch_table(spec.bea_table, years)
    if not blob:
        return []

    candidates = [r for r in blob["rows"] if _matches_line(spec, r)]
    if not candidates and blob["rows"]:
        logger.warning(
            "BEA %s returned rows but none matched line %s (%r) — refusing to guess",
            spec.bea_table, spec.bea_line, spec.bea_expect,
        )
    points = [dp for dp in (_row_to_datapoint(spec, r, blob["url"]) for r in candidates) if dp]
    if year is not None:
        points = [p for p in points if p.observed.year == year]
    # Latest first: for "latest" requests that is the answer, and for a named
    # year there is only one.
    points.sort(key=lambda p: p.observed.year or 0, reverse=True)
    return points[:1]
