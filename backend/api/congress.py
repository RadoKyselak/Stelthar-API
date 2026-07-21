from typing import Dict, Any, List
import httpx
from config.constants import API_TIMEOUTS, RATE_LIMIT_QUOTAS
from config import CONGRESS_API_KEY, logger
from utils.retry import async_retry
from utils.rate_limiter import get_quota_limiter

_congress_limiter = get_quota_limiter("CONGRESS", *RATE_LIMIT_QUOTAS.CONGRESS)

@async_retry(max_attempts=3, exceptions=(httpx.HTTPError, httpx.TimeoutException))
async def query_congress(keyword_query: str) -> List[Dict[str, Any]]:
    if not CONGRESS_API_KEY:
        return [{"error": "CONGRESS_API_KEY missing", "source": "CONGRESS", "status": "failed"}]
    
    if not keyword_query or not keyword_query.strip():
        return []

    await _congress_limiter.acquire()

    params = {"api_key": CONGRESS_API_KEY, "q": keyword_query, "limit": 3}
    url = "https://api.congress.gov/v3/bill"

    try:
        async with httpx.AsyncClient(timeout=API_TIMEOUTS.CONGRESS) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
            bills = data.get("bills", [])
            results = []
            
            for bill in bills:
                title = bill.get('title', 'N/A')
                bill_number = f"{bill.get('type', '')}{bill.get('number', '')}"
                congress_num = bill.get('congress', '')
                latest_action_text = bill.get('latestAction', {}).get('text', 'No recent action text.')
                
                bill_url = None
                if congress_num and bill.get('type') and bill.get('number'):
                    bill_url = (
                        f"https://www.congress.gov/bill/{congress_num}th-congress/"
                        f"{bill.get('type','').lower()}-bill/{bill.get('number','')}"
                    )

                results.append({
                    "title": f"Congress Bill: {title} ({bill_number})",
                    "url": bill_url,
                    "snippet": f"Latest Action: {latest_action_text}"
                })
            return results
            
    except httpx.HTTPStatusError as e:
        logger.error("Congress API HTTP error %s: %s", e.response.status_code, e.response.text)
        return [{"error": f"Congress API error: {e.response.status_code}", "source": "CONGRESS", "status": "failed"}]
    except httpx.RequestError as e:
        logger.error("Congress API request error: %s", str(e))
        return [{"error": str(e), "source": "CONGRESS", "status": "failed"}]
    except Exception as e:
        logger.exception("Unexpected error during Congress query")
        return [{
            "error": f"Unexpected error processing Congress data: {str(e)}",
            "source": "CONGRESS",
            "status": "failed"
        }]
