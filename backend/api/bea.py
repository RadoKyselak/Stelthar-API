import json
from typing import Dict, Any, List
import httpx
from config.constants import API_TIMEOUTS, BEA_CONFIG, RATE_LIMIT_QUOTAS
from config import BEA_API_KEY, logger
from utils.parsing import parse_numeric_value
from utils.retry import async_retry
from utils.rate_limiter import get_quota_limiter

_bea_limiter = get_quota_limiter("BEA", *RATE_LIMIT_QUOTAS.BEA)

@async_retry(max_attempts=3, exceptions=(httpx.HTTPError, httpx.TimeoutException))
async def query_bea(params: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not BEA_API_KEY:
        return [{"error": "BEA_API_KEY missing", "source": "BEA", "status": "failed"}]

    await _bea_limiter.acquire()

    requested_line_code_str = str(params.get("LineCode", "")).strip()
    if not requested_line_code_str:
        logger.warning("BEA query called without a specific LineCode in params.")
        return [{"error": "BEA query missing LineCode", "source": "BEA", "status": "failed"}]
    
    is_requested_code_numeric = requested_line_code_str.isdigit()

    final_params = {
        "UserID": BEA_API_KEY,
        "method": "GetData",
        "ResultFormat": "json",
        "DataSetName": params.get("DataSetName"),
        "TableName": params.get("TableName"),
        "Frequency": params.get("Frequency"),
        "Year": params.get("Year"),
        "LineCode": requested_line_code_str,
        "GeoFips": params.get("GeoFips"),
    }
    api_params = {k: v for k, v in final_params.items() if v is not None}
    url = "https://apps.bea.gov/api/data"

    try:
        async with httpx.AsyncClient(timeout=API_TIMEOUTS.BEA) as client:
            r = await client.get(url, params=api_params)
            r.raise_for_status()
            payload = r.json()
            request_url = str(r.url)
    except httpx.HTTPStatusError as e:
        logger.error("BEA HTTP error %s: %s", e.response.status_code, e.response.text)
        return [{"error": f"BEA API error: {e.response.status_code}", "source": "BEA", "status": "failed"}]
    except httpx.RequestError as e:
        logger.error("BEA request error: %s", str(e))
        return [{"error": str(e), "source": "BEA", "status": "failed"}]
    except json.JSONDecodeError:
        logger.error("BEA returned non-JSON response: %s", r.text[:200])
        return [{"error": "BEA API returned invalid JSON", "source": "BEA", "status": "failed"}]

    results_data = payload.get("BEAAPI", {}).get("Results", {}).get("Data", [])
    out: List[Dict[str, Any]] = []
    
    if not results_data:
        logger.info("BEA returned no data rows for params: %s", api_params)
        return []

    found_match = False
    for item in results_data:
        returned_line_code_obj = item.get("LineCode") or item.get("SeriesCode")
        if returned_line_code_obj is None:
            continue

        returned_line_code_str = str(returned_line_code_obj).strip()

        match = False
        if returned_line_code_str == requested_line_code_str:
            match = True
        elif is_requested_code_numeric:
            known_mappings_t31600 = {
                "2": ["G16007", "G16046"],
                "14": ["G16029", "G16068", "G16107"]
            }
            if params.get("TableName") == "T31600" and requested_line_code_str in known_mappings_t31600:
                if returned_line_code_str in known_mappings_t31600[requested_line_code_str]:
                    match = True

        if not match:
            continue

        found_match = True
        desc = item.get("LineDescription") or item.get("SeriesDescription") or "Data"
        data_value_raw = item.get("DataValue") or ""
        numeric = parse_numeric_value(data_value_raw)
        unit = item.get("Unit") or ""
        unit_multiplier = item.get("UnitMultiplier")
        time_period = item.get("TimePeriod") or final_params.get("Year")
        snippet = f"{desc} ({returned_line_code_str}) for {time_period}: {data_value_raw}{' '+unit if unit else ''}."

        out.append({
            "title": f"BEA: {params.get('DataSetName')}/{params.get('TableName')}",
            "url": request_url,
            "snippet": snippet,
            "data_value": numeric,
            "raw_data_value": data_value_raw,
            "unit": unit,
            "unit_multiplier": unit_multiplier,
            "line_description": desc,
            "line_code": returned_line_code_str,
            "raw_year": item.get("TimePeriod"),
            "raw_geo": item.get("GeoFips"),
        })

    if params.get("TableName") == "T31600" and out:
        for item_out in out:
            if item_out.get("unit_multiplier") is None:
                item_out["unit_multiplier"] = 1000000
            if not item_out.get("unit"):
                item_out["unit"] = "Millions of Dollars"

    if not found_match and results_data:
        logger.warning(
            "BEA returned data for params %s, but no rows matched filter for requested LineCode %s",
            api_params,
            requested_line_code_str
        )

    return out
