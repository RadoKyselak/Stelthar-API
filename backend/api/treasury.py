"""Treasury Fiscal Data API client (keyless).

Docs: https://fiscaldata.treasury.gov/api-documentation/

This fills a gap no other source in the system covers: the U.S. national debt,
as an authoritative first-party figure. The planner emits a `treasury` block
like {"metric": "debt", "year": "2023"} (year optional -> latest available).
"""
from typing import Dict, Any, List, Optional
import httpx

from config import TREASURY_FISCAL_BASE_URL, logger
from config.constants import API_TIMEOUTS, RATE_LIMIT_QUOTAS
from utils.parsing import parse_numeric_value
from utils.retry import async_retry
from utils.rate_limiter import get_quota_limiter

_treasury_limiter = get_quota_limiter("TREASURY", *RATE_LIMIT_QUOTAS.TREASURY)

# metric -> (endpoint path, value field, human label)
_DATASETS: Dict[str, Dict[str, str]] = {
    "debt": {
        "endpoint": "/v2/accounting/od/debt_to_penny",
        "value_field": "tot_pub_debt_out_amt",
        "label": "Total public debt outstanding",
    },
    "debt_annual": {
        "endpoint": "/v2/accounting/od/debt_outstanding",
        "value_field": "debt_outstanding_amt",
        "label": "Debt outstanding (fiscal year end)",
    },
}


def _humanize_usd(value: Optional[float]) -> str:
    if value is None:
        return "N/A"
    a = abs(value)
    if a >= 1e12:
        return f"${value / 1e12:.2f} trillion"
    if a >= 1e9:
        return f"${value / 1e9:.2f} billion"
    if a >= 1e6:
        return f"${value / 1e6:.2f} million"
    return f"${value:,.0f}"


@async_retry(max_attempts=3, exceptions=(httpx.HTTPError, httpx.TimeoutException))
async def query_treasury(params: Dict[str, Any]) -> List[Dict[str, Any]]:
    metric = str(params.get("metric", "debt")).strip().lower()
    year = str(params.get("year", "")).strip()

    dataset = _DATASETS.get(metric) or _DATASETS.get("debt")
    value_field = dataset["value_field"]

    await _treasury_limiter.acquire()

    query = {
        "fields": f"record_date,record_fiscal_year,{value_field}",
        "sort": "-record_date",
        "page[size]": "1",
        "format": "json",
    }
    if year.isdigit():
        # Prefer fiscal-year filtering; fall back to a calendar window if empty.
        query["filter"] = f"record_fiscal_year:eq:{year}"

    url = f"{TREASURY_FISCAL_BASE_URL}{dataset['endpoint']}"

    try:
        async with httpx.AsyncClient(timeout=API_TIMEOUTS.TREASURY) as client:
            r = await client.get(url, params=query)
            r.raise_for_status()
            data = r.json()
            request_url = str(r.url)

            rows = data.get("data", []) if isinstance(data, dict) else []

            # If a fiscal-year filter returned nothing, retry against a calendar
            # window (some datasets key off calendar record_date instead).
            if not rows and year.isdigit():
                query.pop("filter", None)
                query["filter"] = f"record_date:gte:{year}-01-01,record_date:lte:{year}-12-31"
                r = await client.get(url, params=query)
                r.raise_for_status()
                data = r.json()
                request_url = str(r.url)
                rows = data.get("data", []) if isinstance(data, dict) else []

            if not rows:
                logger.info("Treasury returned no rows for metric=%s year=%s", metric, year or "latest")
                return []

            row = rows[0]
            raw = row.get(value_field)
            numeric = parse_numeric_value(raw)
            record_date = row.get("record_date", "")
            record_fy = row.get("record_fiscal_year", "")
            snippet = (
                f"{dataset['label']} as of {record_date}"
                f"{f' (FY{record_fy})' if record_fy else ''}: {_humanize_usd(numeric)}."
            )

            return [{
                "title": f"U.S. Treasury: {dataset['label']}",
                "url": request_url,
                "snippet": snippet,
                "data_value": numeric,
                "raw_data_value": str(raw) if raw is not None else "N/A",
                "unit": "USD",
                "unit_multiplier": 1,
                "line_description": dataset["label"],
                "record_date": record_date,
                "year": str(record_fy) if record_fy else (record_date[:4] if record_date else year),
                "source": "TREASURY",
            }]

    except httpx.HTTPStatusError as e:
        logger.error("Treasury HTTP error %s: %s", e.response.status_code, e.response.text[:200])
        return [{"error": f"Treasury API error: {e.response.status_code}", "source": "TREASURY", "status": "failed"}]
    except httpx.RequestError as e:
        logger.error("Treasury request error: %s", str(e))
        return [{"error": str(e), "source": "TREASURY", "status": "failed"}]
    except Exception as e:
        logger.exception("Unexpected error during Treasury query")
        return [{"error": f"Unexpected error processing Treasury data: {str(e)}", "source": "TREASURY", "status": "failed"}]
