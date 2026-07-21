import json
from typing import Dict, Any, List
import httpx
from config.constants import API_TIMEOUTS, RATE_LIMIT_QUOTAS, BLS_SERIES_CATALOG
from config import BLS_API_KEY, logger
from utils.parsing import parse_numeric_value
from utils.retry import async_retry
from utils.rate_limiter import get_quota_limiter

_bls_limiter = get_quota_limiter("BLS", *RATE_LIMIT_QUOTAS.BLS)

@async_retry(max_attempts=3, exceptions=(httpx.HTTPError, httpx.TimeoutException))
async def query_bls(params: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not BLS_API_KEY:
        return [{"error": "BLS_API_KEY missing", "source": "BLS", "status": "failed"}]

    await _bls_limiter.acquire()

    metric = params.get("metric")
    year_str = params.get("year")
    
    if not metric or not year_str:
        return [{"error": "BLS query missing metric or year", "source": "BLS", "status": "failed"}]

    try:
        year_int = int(year_str)
    except ValueError:
        return [{"error": "BLS query invalid year", "source": "BLS", "status": "failed"}]

    # Expanded well beyond the original 2 series (national CPI + unemployment).
    # Lookup is case-insensitive so "CPI" and "cpi" both resolve.
    spec = BLS_SERIES_CATALOG.get(str(metric).strip().lower())
    if not spec:
        return [{
            "error": f"BLS metric '{metric}' not supported. Available: {', '.join(sorted(BLS_SERIES_CATALOG))}",
            "source": "BLS",
            "status": "failed",
        }]

    series_id = spec["series_id"]
    metric_kind = spec["kind"]
    metric_label = spec["label"]

    # Index metrics need the prior year to compute a year-over-year change.
    start_year = str(year_int - 1) if metric_kind == "cpi" else year_str
    end_year = year_str

    url = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
    payload = json.dumps({
        "seriesid": [series_id],
        "startyear": start_year,
        "endyear": end_year,
        "registrationKey": BLS_API_KEY,
        "annualaverage": True
    })
    headers = {'Content-Type': 'application/json'}

    try:
        async with httpx.AsyncClient(timeout=API_TIMEOUTS.BLS) as client:
            r = await client.post(url, headers=headers, content=payload)
            r.raise_for_status()
            data = r.json()

            if data.get("status") != "REQUEST_SUCCEEDED":
                message = data.get("message", ["Unknown BLS error."])[0]
                logger.error(f"BLS API error: {message}")
                return [{"error": f"BLS API error: {message}", "source": "BLS", "status": "failed"}]
    except httpx.HTTPStatusError as e:
        logger.error("BLS HTTP error %s: %s", e.response.status_code, e.response.text)
        return [{"error": f"BLS API error: {e.response.status_code}", "source": "BLS", "status": "failed"}]
    except httpx.RequestError as e:
        logger.error("BLS request error: %s", str(e))
        return [{"error": str(e), "source": "BLS", "status": "failed"}]
    except json.JSONDecodeError:
        logger.error("BLS returned non-JSON response: %s", r.text[:200])
        return [{"error": "BLS API returned invalid JSON", "source": "BLS", "status": "failed"}]

    try:
        series_data = data.get("Results", {}).get("series", [])
        if not series_data:
            logger.warning("BLS returned no data for series %s, years %s-%s", series_id, start_year, end_year)
            return [{"error": "BLS returned no data for series", "source": "BLS", "status": "failed"}]
        
        annual_data = series_data[0].get("data", [])
        series_url = f"https://data.bls.gov/timeseries/{series_id}"
        results = []

        def _annual(y: str):
            """Annual average (period M13) for a year, else the latest monthly point."""
            m13 = [d for d in annual_data if d.get("year") == y and d.get("period") == "M13"]
            if m13:
                return m13[0], True
            monthly = [d for d in annual_data if d.get("year") == y and str(d.get("period", "")).startswith("M")]
            if monthly:
                # BLS returns newest-first; take the most recent month available.
                return monthly[0], False
            return None, False

        if metric_kind == "cpi":
            cur, cur_is_annual = _annual(year_str)
            prev, _ = _annual(start_year)

            if not cur or not prev:
                logger.warning("BLS %s data incomplete for %s", metric, year_str)
                return [{
                    "error": f"BLS {metric_label} data incomplete for {year_str}",
                    "source": "BLS",
                    "status": "failed",
                }]

            current_val = parse_numeric_value(cur.get("value"))
            prev_val = parse_numeric_value(prev.get("value"))

            if current_val is None or prev_val is None or prev_val == 0:
                return [{
                    "error": f"BLS {metric_label} values unusable for {year_str}",
                    "source": "BLS",
                    "status": "failed",
                }]

            change_pct = ((current_val - prev_val) / prev_val) * 100
            basis = "annual average" if cur_is_annual else f"through {cur.get('periodName', 'latest month')}"
            snippet = (
                f"{metric_label} index {year_str} ({basis}): {current_val:.1f}; "
                f"{start_year}: {prev_val:.1f}. Year-over-year change: {change_pct:.2f}%."
            )
            results.append({
                "title": f"BLS {metric_label} {year_str}",
                "url": series_url,
                "snippet": snippet,
                "data_value": change_pct,
                "raw_data_value": f"{change_pct:.2f}",
                "raw_index_current": current_val,
                "raw_index_prev": prev_val,
                "unit": "%",
                "line_description": f"{metric_label} year-over-year change",
                "year": year_str,
                "is_annual_average": cur_is_annual,
                "source": "BLS",
            })

        else:  # kind == "rate": read the level directly
            point, is_annual = _annual(year_str)

            # "Old data" guard: if the requested year has no data yet, fall back to
            # the most recent available point and label the year explicitly rather
            # than returning nothing (which used to end the whole verification).
            fallback_used = False
            if not point and annual_data:
                point = annual_data[0]
                is_annual = point.get("period") == "M13"
                fallback_used = True

            if not point:
                logger.warning("BLS %s data not found for %s", metric, year_str)
                return [{
                    "error": f"BLS {metric_label} data not found for {year_str}",
                    "source": "BLS",
                    "status": "failed",
                }]

            value = parse_numeric_value(point.get("value"))
            actual_year = point.get("year", year_str)
            basis = "annual average" if is_annual else point.get("periodName", "monthly")
            note = (
                f" (requested {year_str}; latest available is {actual_year})"
                if fallback_used and actual_year != year_str else ""
            )
            snippet = (
                f"{metric_label} in {actual_year} ({basis}): {value}{spec.get('unit', '')}{note}."
            )
            results.append({
                "title": f"BLS {metric_label} {actual_year}",
                "url": series_url,
                "snippet": snippet,
                "data_value": value,
                "raw_data_value": str(value),
                "unit": spec.get("unit", ""),
                "line_description": metric_label,
                "year": str(actual_year),
                "is_annual_average": is_annual,
                "year_fallback": fallback_used,
                "source": "BLS",
            })

        return results

    except Exception as e:
        logger.exception("Error processing BLS data")
        return [{
            "error": f"Error processing BLS data: {str(e)}",
            "source": "BLS",
            "status": "failed"
        }]
