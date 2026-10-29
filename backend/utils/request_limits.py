"""Per-visitor request limits, checked before any work is done.

Checks that use only free government data still spend something scarce: the
agencies' daily API quotas, and this service's time. One visitor sending
thousands of requests could use them up for everyone. So each client IP gets
a per-minute and a per-day allowance for checks and for lookups.

Counters share the spend guard's store: Upstash Redis when configured (one
limit across every serverless instance), otherwise per instance. Unlike the
spend guard, a store outage fails OPEN here: refusing every free check because
a counter is unreachable would be an outage of our own making, and the paid
tier stays behind its own fail-closed limits.
"""
import hashlib
import time
from typing import Optional

from fastapi import HTTPException, Request

import config
from config import logger
from utils import spend_guard

_KINDS = {
    "verify": lambda: (config.VERIFY_PER_MINUTE, config.VERIFY_PER_DAY),
    "lookup": lambda: (config.LOOKUP_PER_MINUTE, config.LOOKUP_PER_DAY),
}


def client_ip(request: Request) -> str:
    """The caller's IP as Vercel reports it. Vercel overwrites these headers at
    its edge, so a client cannot spoof them to dodge a per-client limit."""
    for h in ("x-vercel-forwarded-for", "x-real-ip", "x-forwarded-for"):
        v = request.headers.get(h)
        if v:
            return v.split(",")[0].strip()
    return request.client.host if request.client else ""


def _who(ip: str) -> str:
    # The raw IP is never stored.
    return hashlib.sha256(f"rl|{ip}".encode()).hexdigest()[:20]


async def retry_after(kind: str, ip: str) -> Optional[int]:
    """Seconds to wait if this request is over a limit, else None (and counted)."""
    per_minute, per_day = _KINDS[kind]()
    now = time.time()
    minute, day = int(now // 60), time.strftime("%Y-%m-%d", time.gmtime(now))
    who = _who(ip)
    store = spend_guard.get_store()
    try:
        m = await store.incr(f"rl:{kind}:{who}:m:{minute}", ttl=120)
        if m > per_minute:
            return 60 - int(now % 60) or 1
        d = await store.incr(f"rl:{kind}:{who}:d:{day}", ttl=2 * 24 * 3600)
        if d > per_day:
            return 86400 - int(now % 86400) or 1
    except Exception as e:  # noqa: BLE001 — see module docstring: fail open
        logger.warning("Request limit store unavailable: %s", str(e)[:120])
    return None


def limit(kind: str):
    """A route dependency: 429 with Retry-After once a client is over its limit."""
    async def _dep(request: Request) -> None:
        wait = await retry_after(kind, client_ip(request))
        if wait is not None:
            raise HTTPException(
                status_code=429,
                detail="Too many requests from this connection. Try again shortly.",
                headers={"Retry-After": str(wait)},
            )
    return _dep
