import json
from typing import Dict, Any, List
import httpx
from config.constants import API_TIMEOUTS, RATE_LIMIT_QUOTAS
from config import CENSUS_API_KEY, logger
from utils.parsing import parse_numeric_value
from utils.urls import public_url
from utils.retry import async_retry
from utils.rate_limiter import get_quota_limiter

_census_limiter = get_quota_limiter("CENSUS", *RATE_LIMIT_QUOTAS.CENSUS)

@async_retry(max_attempts=3, exceptions=(httpx.HTTPError, httpx.TimeoutException))
async def query_census_acs(params: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not CENSUS_API_KEY:
        return [{"error": "CENSUS_API_KEY missing", "source": "CENSUS", "status": "failed"}]
    
    await _census_limiter.acquire()
    
    required_keys = ["year", "dataset", "get", "for"]
    if not all(k in params and params[k] for k in required_keys):
        logger.warning("Census ACS query missing required params: %s", params)
        return []

    year = params["year"]
    dataset = params["dataset"].strip("/")

    url = f"https://api.census.gov/data/{year}/{dataset}"
    final_params = {
        "key": CENSUS_API_KEY,
        "get": params["get"],
        "for": params["for"]
    }

    try:
        async with httpx.AsyncClient(timeout=API_TIMEOUTS.CENSUS) as client:
            r = await client.get(url, params=final_params)
            if r.status_code == 204:
                logger.info("Census query returned status 204 No Content for params: %s", final_params)
                return []
            r.raise_for_status()
            if "application/json" not in r.headers.get("content-type", "").lower():
                logger.error(
                    "Census returned non-JSON content-type: %s. Response: %s",
                    r.headers.get("content-type"),
                    r.text[:200]
                )
                return [{
                    "error": f"Census API returned non-JSON content-type: {r.headers.get('content-type')}",
                    "source": "CENSUS",
                    "status": "failed"
                }]

            data = r.json()

            if not isinstance(data, list) or len(data) < 2:
                logger.info(
                    "Census query returned no data rows or invalid format for params: %s. Response: %s",
                    final_params,
                    data
                )
                return []

            headers = data[0]
            rows = data[1:]
            results = []

            for row in rows:
                try:
                    row_data = dict(zip(headers, row))
                except TypeError:
                    logger.warning("Failed to zip Census headers and row. Headers: %s, Row: %s", headers, row)
                    continue

                snippet_parts = []
                for k, v in row_data.items():
                    if k and k.upper() not in ["KEY", "FOR", "IN", "STATE", "COUNTY", "US"]:
                        snippet_parts.append(f"{k}: {v}")

                geo_name = row_data.get('NAME', params['for'])
                snippet = f"Data for {geo_name}: " + ", ".join(snippet_parts)

                get_vars = params["get"].split(',')
                primary_var = get_vars[1] if len(get_vars) > 1 else get_vars[0]
                primary_var = primary_var.strip()

                data_value_raw = row_data.get(primary_var)
                numeric_val = parse_numeric_value(data_value_raw)

                results.append({
                    "title": f"Census ACS: {primary_var} for {geo_name}",
                    "url": public_url(r.url),
                    "snippet": snippet,
                    "data_value": numeric_val,
                    "raw_data_value": data_value_raw,
                    "raw_census_row": row_data
                })
            return results

    except httpx.HTTPStatusError as e:
        logger.error("Census HTTP error %s: %s", e.response.status_code, e.response.text)
        return [{"error": f"Census API error: {e.response.status_code}", "source": "CENSUS", "status": "failed"}]
    except httpx.RequestError as e:
        logger.error("Census request error: %s", str(e))
        return [{"error": str(e), "source": "CENSUS", "status": "failed"}]
    except json.JSONDecodeError:
        logger.error("Census returned invalid JSON: %s", r.text[:200])
        return [{"error": "Census API returned invalid JSON", "source": "CENSUS", "status": "failed"}]
    except Exception as e:
        logger.exception("Unexpected error during Census query")
        return [{
            "error": f"Unexpected error processing Census data: {str(e)}",
            "source": "CENSUS",
            "status": "failed"
        }]
