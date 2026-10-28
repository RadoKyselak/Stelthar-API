"""Census ACS — demographics, with the vintage and place read from the payload.

The v2 engine parsed every Census metric (population, median household income,
poverty, uninsured rate, home value, education) and then had nowhere to send
it, so "the U.S. population was about 335 million in 2023" came back
"No verdict". This adapter closes that gap using the ACS 1-year estimates —
the series Census itself headlines for nation- and state-level figures.

Three corrections relative to the legacy adapter and the old metric registry:

  * Rates come from the published subject-table percentages (S1701, S2701,
    S1501). The registry previously pointed "poverty rate" at a raw count
    (B17001_002E) and "uninsured rate" at a single age/sex cell of B27001 —
    numbers that are not rates at all.
  * The geography is read from the row Census returns (NAME + FIPS), never
    copied from the request, so the datapoint guard can compare them.
  * ACS figures are survey estimates. The 90% margin of error is fetched with
    the estimate and carried on the datapoint, so a claim inside the margin is
    not called wrong on sampling noise.

Census now redirects every keyless data request to a "missing key" page, so a
302 is reported as an outage (SourceUnavailable), not as "not published".
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from config import CENSUS_API_KEY, logger
from config.constants import API_TIMEOUTS, RATE_LIMIT_QUOTAS
from evidence.cache import TTL_IMMUTABLE, cached
from evidence.datapoint import ANNUAL, Datapoint, Geography, Period
from evidence.sources import SourceUnavailable
from evidence.units import Quantity, parse_number
from utils.rate_limiter import get_quota_limiter
from utils.urls import public_url

_limiter = get_quota_limiter("CENSUS", *RATE_LIMIT_QUOTAS.CENSUS)
_BASE = "https://api.census.gov/data"

# Census encodes "no estimate / controlled / not applicable" as large negative
# sentinels (-555555555, -666666666, -888888888, -999999999). Never a value.
_SENTINEL_FLOOR = -100_000_000

# ACS 1-year estimates are released each September for the prior year, and
# 2020 was never released as a standard product. Present-tense claims walk
# back from last year until a vintage exists.
_LOOKBACK_YEARS = 4


def _moe_var(variable: str) -> Optional[str]:
    return variable[:-1] + "M" if variable.endswith("E") else None


def _table_id(variable: str) -> str:
    return variable.split("_", 1)[0]


def table_permalink(variable: str, dataset: str, year: int, geography: Geography) -> str:
    """A data.census.gov link a reader can open, rather than a raw API call."""
    prefix = "ACSST1Y" if dataset.endswith("subject") else "ACSDT1Y"
    if geography.level == "state" and geography.fips:
        g = f"040XX00US{geography.fips}"
    else:
        g = "010XX00US"
    return f"https://data.census.gov/table/{prefix}{year}.{_table_id(variable)}?g={g}"


def _for_clause(geography: Optional[Geography]) -> str:
    if geography is not None and geography.level == "state" and geography.fips:
        return f"state:{geography.fips}"
    return "us:1"


async def _fetch_row(year: int, dataset: str, variables: List[str], geo_for: str):
    """One Census row as a dict, {} when the vintage does not exist."""
    params: Dict[str, str] = {"get": ",".join(["NAME", *variables]), "for": geo_for}
    if CENSUS_API_KEY:
        params["key"] = CENSUS_API_KEY
    url = f"{_BASE}/{year}/{dataset}"

    async def _load():
        await _limiter.acquire()
        try:
            async with httpx.AsyncClient(
                timeout=API_TIMEOUTS.CENSUS, follow_redirects=False
            ) as client:
                r = await client.get(url, params=params)
        except httpx.HTTPError as e:
            raise SourceUnavailable("CENSUS", f"request failed: {e}") from e

        if r.status_code in (301, 302, 303, 307, 308):
            where = r.headers.get("location", "")
            reason = "API key missing or invalid" if "key" in where.lower() else f"redirected to {where}"
            raise SourceUnavailable("CENSUS", reason)
        if r.status_code in (204, 404):
            # 404: that vintage/dataset is not published. 204: no rows.
            return None
        if r.status_code >= 400:
            body = (r.text or "")[:200]
            if "unknown variable" in body.lower() or "error: " in body.lower():
                logger.warning("Census rejected query %s: %s", params.get("get"), body)
                return None
            raise SourceUnavailable("CENSUS", f"HTTP {r.status_code}")
        try:
            data = r.json()
        except ValueError as e:
            raise SourceUnavailable("CENSUS", "non-JSON response") from e
        if not isinstance(data, list) or len(data) < 2:
            return None
        return {"header": data[0], "row": data[1], "url": public_url(r.url)}

    # A published ACS vintage is never revised, so the TTL can be long.
    return await cached("census", f"{year}:{dataset}:{params['get']}:{geo_for}",
                        TTL_IMMUTABLE, _load)


def _row_to_datapoint(spec, year: int, dataset: str, blob: Dict[str, Any]) -> Optional[Datapoint]:
    header, row = blob["header"], blob["row"]
    try:
        rec = dict(zip(header, row))
    except TypeError:
        return None

    raw = rec.get(spec.census_variable)
    value = parse_number(raw)
    if value is None or value <= _SENTINEL_FLOOR:
        return None

    moe_name = _moe_var(spec.census_variable)
    moe = parse_number(rec.get(moe_name)) if moe_name else None
    if moe is not None and (moe <= _SENTINEL_FLOOR or moe < 0):
        moe = None  # controlled estimate: published without a sampling margin

    name = str(rec.get("NAME") or "").strip()
    if "state" in rec and rec.get("state"):
        geography = Geography(name=name or "unknown state", level="state",
                              fips=str(rec["state"]).zfill(2))
    elif "us" in rec:
        geography = Geography.national() if not name or name == "United States" \
            else Geography(name=name, level="nation")
    else:
        return None

    extra: Dict[str, Any] = {"dataset": dataset, "api_url": blob.get("url", "")}
    if moe:
        extra["margin_of_error"] = moe
        extra["caveat"] = (
            "ACS figures are survey estimates; the published 90% margin of error "
            f"is ±{_fmt(moe, spec.family)}."
        )

    return Datapoint(
        quantity=Quantity(magnitude=value, family=spec.family, raw=str(raw)),
        metric=spec.metric,
        label=spec.label,
        observed=Period(year=year, kind=ANNUAL, basis="ACS 1-year estimate"),
        geography=geography,
        source="CENSUS",
        source_url=table_permalink(spec.census_variable, dataset, year, geography),
        series_id=spec.census_variable,
        raw_value=str(raw),
        extra=extra,
    )


def _fmt(v: float, family: str) -> str:
    from evidence.units import humanize
    return humanize(v, family)


async def fetch(spec, year: Optional[int], geography: Optional[Geography]) -> List[Datapoint]:
    """ACS estimate for a metric at the nation or one state.

    `year=None` means "latest published": walk back from last year until a
    vintage answers. A named year that is not published returns [], which the
    pipeline reports as "not published for that period".
    """
    dataset = spec.census_dataset or "acs/acs1"
    variables = [spec.census_variable]
    moe = _moe_var(spec.census_variable)
    if moe:
        variables.append(moe)
    geo_for = _for_clause(geography)

    if year is not None:
        years = [year]
    else:
        this_year = datetime.now(timezone.utc).year
        years = [this_year - k for k in range(1, _LOOKBACK_YEARS + 1)]

    for y in years:
        if y == 2020:
            continue  # no standard 2020 ACS 1-year release
        blob = await _fetch_row(y, dataset, variables, geo_for)
        if not blob:
            continue
        dp = _row_to_datapoint(spec, y, dataset, blob)
        if dp:
            return [dp]
    return []
