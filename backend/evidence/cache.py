"""Disk-backed response cache — the cheapest way to multiply a free quota.

Every source this system uses is rate-limited, and the limits are lower than
they look. BLS without a registration key allows 25 requests per day, which one
eval run exhausts. Gemini's free tier allows a few hundred. The government
figures behind them change once a month at most, and a debt figure for 2023 will
never change again.

So the binding constraint is requests, not correctness, and a cache is not an
optimisation here — it is what makes repeated runs possible at all. It also
makes the eval reproducible: a second run measures the same inputs rather than
whatever the quota allowed that day.

Two TTLs, because two kinds of value:
  * A closed period (2023's annual average) is immutable. Cache it for a month.
  * An open one (today's debt figure) moves daily. Cache it for an hour.

Cache reads never raise. A corrupt or unreadable entry is a miss, not an error —
degrading to a live request is always safe, whereas failing a verification
because a cache file was truncated is not.
"""
import hashlib
import json
import os
import pathlib
import time
from typing import Any, Awaitable, Callable, Optional

from config import logger

# Closed periods never change; open ones move daily.
TTL_IMMUTABLE = 30 * 24 * 3600
TTL_VOLATILE = 3600

_CACHE_DIR = pathlib.Path(
    os.getenv("STELTHAR_CACHE_DIR", pathlib.Path(__file__).resolve().parent.parent / ".cache")
)


def _path_for(namespace: str, key: str) -> pathlib.Path:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return _CACHE_DIR / namespace / f"{digest}.json"


def read(namespace: str, key: str, ttl: int) -> Optional[Any]:
    """Return a cached payload, or None on miss/expiry/corruption."""
    p = _path_for(namespace, key)
    try:
        if not p.exists():
            return None
        raw = json.loads(p.read_text())
        if time.time() - raw.get("ts", 0) > ttl:
            return None
        return raw.get("payload")
    except Exception:
        # A bad cache entry must never be able to fail a verification.
        return None


def write(namespace: str, key: str, payload: Any) -> None:
    p = _path_for(namespace, key)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"ts": time.time(), "payload": payload}))
        tmp.replace(p)  # atomic, so a reader never sees a half-written file
    except Exception as e:
        logger.debug("Cache write failed for %s/%s: %s", namespace, key, e)


async def cached(
    namespace: str,
    key: str,
    ttl: int,
    loader: Callable[[], Awaitable[Any]],
) -> Any:
    """Return a cached payload or await `loader` and cache what it returns.

    An empty result is deliberately NOT cached: an empty list from a
    rate-limited or failed request would otherwise be served for the whole TTL,
    turning a transient outage into a persistent "no data" answer.
    """
    hit = read(namespace, key, ttl)
    if hit is not None:
        return hit
    payload = await loader()
    if payload:
        write(namespace, key, payload)
    return payload


def stats() -> dict:
    """Entry count and size per namespace, for the preflight report."""
    out = {}
    if not _CACHE_DIR.exists():
        return out
    for ns in sorted(p for p in _CACHE_DIR.iterdir() if p.is_dir()):
        files = list(ns.glob("*.json"))
        out[ns.name] = {
            "entries": len(files),
            "bytes": sum(f.stat().st_size for f in files),
        }
    return out
