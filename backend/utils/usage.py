"""How many checks the service has made: counts by day and kind, nothing else.

There was no way to say how many claims had been checked. The only counters
were the spend guard's, which count paid AI calls, expire in two days, and
live in each server instance's memory unless a shared store is connected.
Runtime logs on the hosting plan are kept for an hour.

So each finished check adds one to two counters in the same store the spend
guard uses: one for its day, kept 400 days, and one running total. A key
names a kind of check and nothing about it:

    claim            one claim, through /v2/verify, /verify or /v1/verify
    passage          a pasted or highlighted passage, through /v2/check
    page             a page checked by its link
    claims           the claims inside passages and pages, one per claim
    verdict:<key>    how each claim came out (supported, contradicted, ...)
    lookup           a figure looked up
    share            a shared check's page opened
    mcp:<tool>       an assistant's tool call
    ai_calls, ai_spend_micro   paid model calls and their cost in millionths of a dollar

No claim's text, no address, no visitor is recorded. Counting never fails a
request: a store that cannot be reached loses the count, not the answer.

The counts are reliable only with the shared store (Upstash Redis, through
UPSTASH_REDIS_REST_URL/TOKEN or Vercel's KV_REST_API_URL/TOKEN). Without it
each instance counts alone and forgets when it stops; the report says so.
"""
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List

from config import logger

_KEEP_DAYS = 400 * 24 * 3600
_FOREVER = 10 * 365 * 24 * 3600
KINDS = ("claim", "passage", "page", "claims", "lookup", "share", "ai_calls", "ai_spend_micro")
VERDICT_KEYS = ("supported", "mostly-accurate", "contradicted", "misleading", "no-verdict", "unavailable")
MCP_TOOLS = ("check_text", "check_page", "check_claim", "find_figure")


def _day(d: date = None) -> str:
    return (d or datetime.now(timezone.utc).date()).isoformat()


async def count(kind: str, n: int = 1) -> None:
    """Add `n` to today's count and the running total for `kind`. Never raises."""
    if n <= 0:
        return
    from utils import spend_guard
    try:
        store = spend_guard.get_store()
        await store.incr(f"use:{_day()}:{kind}", n, ttl=_KEEP_DAYS)
        await store.incr(f"use:all:{kind}", n, ttl=_FOREVER)
    except Exception as e:  # noqa: BLE001 — a count is never worth a failed answer
        logger.debug("Usage count for %s lost: %s", kind, e)


async def count_verdicts(keys: Iterable[str]) -> None:
    tally: Dict[str, int] = {}
    for k in keys:
        k = k if k in VERDICT_KEYS else "no-verdict"
        tally[k] = tally.get(k, 0) + 1
    for k, n in tally.items():
        await count(f"verdict:{k}", n)


def _all_kinds() -> List[str]:
    return list(KINDS) + [f"verdict:{k}" for k in VERDICT_KEYS] + [f"mcp:{t}" for t in MCP_TOOLS]


async def report(days: int = 30) -> Dict[str, object]:
    """Counts for the last `days` days and since counting began."""
    from utils import spend_guard
    store = spend_guard.get_store()
    kinds = _all_kinds()
    today = datetime.now(timezone.utc).date()
    out: Dict[str, object] = {
        "store": store.kind,
        "reliable": store.kind.startswith("redis"),
        "note": ("" if store.kind.startswith("redis") else
                 "No shared store is connected: these are one server instance's counts since it started, "
                 "not the service's. Connect Upstash Redis in Vercel for real totals."),
    }
    try:
        totals = {k: await store.get(f"use:all:{k}") for k in kinds}
        by_day = []
        for i in range(days):
            d = _day(today - timedelta(days=i))
            row = {k: await store.get(f"use:{d}:{k}") for k in kinds}
            row = {k: v for k, v in row.items() if v}
            if row:
                by_day.append({"day": d, **row})
    except Exception as e:  # noqa: BLE001
        out["error"] = f"the store could not be read: {type(e).__name__}"
        return out
    totals = {k: v for k, v in totals.items() if v}
    if "ai_spend_micro" in totals:
        totals["ai_spend_usd"] = round(totals.pop("ai_spend_micro") / 1e6, 4)
    for row in by_day:
        if "ai_spend_micro" in row:
            row["ai_spend_usd"] = round(row.pop("ai_spend_micro") / 1e6, 4)
    out["totals"] = totals
    out["by_day"] = by_day
    out["claims_checked"] = totals.get("claim", 0) + totals.get("claims", 0)
    return out
