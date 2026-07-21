from typing import Dict, List
import httpx
from config import logger
from config.constants import API_TIMEOUTS, RATE_LIMIT_QUOTAS
from utils.retry import async_retry
from utils.rate_limiter import get_quota_limiter

_datagov_limiter = get_quota_limiter("DATA_GOV", *RATE_LIMIT_QUOTAS.DATA_GOV)

@async_retry(max_attempts=3, exceptions=(httpx.HTTPError, httpx.TimeoutException))
async def query_datagov(keyword_query: str) -> List[Dict[str, str]]:
    if not keyword_query or not keyword_query.strip():
        return []

    await _datagov_limiter.acquire()

    url = "https://catalog.data.gov/api/3/action/package_search"
    params = {"q": keyword_query, "rows": 5}

    try:
        async with httpx.AsyncClient(timeout=API_TIMEOUTS.DATA_GOV) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
            results_list = data.get("result", {}).get("results", [])
            out = []
            
            for item in results_list:
                title = item.get('title', 'N/A')
                notes = (item.get("notes") or "")[:300]
                resources = item.get("resources") or []
                resource_url = None
                
                if resources and isinstance(resources, list):
                    preferred_formats = ['csv', 'json', 'xls', 'xlsx', 'zip', 'pdf']
                    found_url = None
                    for res in resources:
                        res_url = res.get('url')
                        res_format = (res.get('format') or '').lower()
                        if res_url and any(fmt in res_format for fmt in preferred_formats):
                            found_url = res_url
                            break
                    if not found_url and resources:
                        resource_url = resources[0].get('url')
                    else:
                        resource_url = found_url

                dataset_page = f"https://catalog.data.gov/dataset/{item.get('name')}" if item.get("name") else None

                final_url = None
                if isinstance(resource_url, str) and resource_url.startswith("http"):
                    final_url = resource_url
                elif dataset_page:
                    final_url = dataset_page

                out.append({
                    "title": f"Data.gov: {title}",
                    "url": final_url,
                    "snippet": notes
                })
            return out
            
    except httpx.HTTPStatusError as e:
        logger.error("Data.gov API HTTP error %s: %s", e.response.status_code, e.response.text)
        return [{"error": f"Data.gov API error: {e.response.status_code}", "source": "DATA_GOV", "status": "failed"}]
    except httpx.RequestError as e:
        logger.error("Data.gov API request error: %s", str(e))
        return [{"error": str(e), "source": "DATA_GOV", "status": "failed"}]
    except Exception as e:
        logger.exception("Unexpected error during Data.gov query")
        return [{
            "error": f"Unexpected error processing Data.gov data: {str(e)}",
            "source": "DATA_GOV",
            "status": "failed"
        }]
