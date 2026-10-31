"""Hard limits on AI spend. Every AI call must get a reservation from here first.

The API is public and unauthenticated — the Chrome extension calls it from
users' browsers — so anyone can send it requests. Exact-figure claims cost
nothing (free government APIs). The AI search tier costs money per call, so it
sits behind four independent limits, each checked BEFORE a request is sent:

  1. Daily spend cap (USD). Checked against spend so far PLUS the worst-case
     cost of the next call, so a call that could cross the cap is refused.
  2. Daily call cap, across all users.
  3. Daily call cap per client (hashed IP — the raw IP is never stored).
  4. Per-call caps (searches, output tokens, claim length) in config.

Counters live in Upstash Redis when configured, so the limits hold across all
serverless instances. Without Redis they are per instance: still enforced, but
Vercel can run several instances at once, so the real ceiling is a multiple.
The Anthropic Console workspace spend limit is the backstop above all of this.

Fails CLOSED: if Redis is configured but unreachable, no AI call is made.
"""
import hashlib
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional

import httpx

import config
from config import logger

# USD per million tokens (input, output) for prompts <= 100K tokens, and per
# web search. Unknown models are priced as the most expensive one, so an
# accidental model change makes the caps stricter, never looser.
_PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}
_FALLBACK_PRICE = (10.00, 50.00)
WEB_SEARCH_USD = 0.01
# Search results are read as input tokens. Generous upper bound per search.
_WORST_CASE_INPUT_TOKENS_PER_SEARCH = 40_000
_BASE_INPUT_TOKENS = 3_000

_DAY_TTL = 2 * 24 * 3600


def estimate_cost(model: str, input_tokens: int, output_tokens: int, searches: int) -> float:
    pin, pout = _PRICES.get(model, _FALLBACK_PRICE)
    if model == "claude-haiku-5-5" and input_tokens > 100_000:
        pin, pout = 0.50, 2.50
    return (input_tokens * pin + output_tokens * pout) / 1e6 + searches * WEB_SEARCH_USD


def worst_case_call_cost(model: Optional[str] = None) -> float:
    model = model or config.CLAUDE_SEARCH_MODEL
    searches = config.AI_MAX_SEARCHES_PER_CALL
    inp = _BASE_INPUT_TOKENS + searches * _WORST_CASE_INPUT_TOKENS_PER_SEARCH
    return estimate_cost(model, inp, config.AI_MAX_OUTPUT_TOKENS, searches)


def client_key(ip: Optional[str]) -> str:
    """A non-reversible id for rate limiting. The raw IP is never stored."""
    if not ip:
        return ""
    salt = (config.REDIS_REST_TOKEN or "stelthar")[:16]
    return hashlib.sha256(f"{salt}:{ip}".encode()).hexdigest()[:24]


class StoreUnavailable(Exception):
    pass


class _MemoryStore:
    """Per-instance counters. Used only when Redis is not configured."""

    kind = "memory (per instance)"

    def __init__(self) -> None:
        self._data: Dict[str, int] = {}
        self._expiry: Dict[str, float] = {}

    def _gc(self) -> None:
        now = time.time()
        for k in [k for k, t in self._expiry.items() if t < now]:
            self._data.pop(k, None)
            self._expiry.pop(k, None)

    async def incr(self, key: str, by: int = 1, ttl: int = _DAY_TTL) -> int:
        self._gc()
        self._data[key] = self._data.get(key, 0) + by
        self._expiry.setdefault(key, time.time() + ttl)
        return self._data[key]

    async def get(self, key: str) -> int:
        self._gc()
        return self._data.get(key, 0)

    async def get_str(self, key: str) -> Optional[str]:
        return None  # answers are cached on disk instead (evidence.cache)

    async def set_str(self, key: str, value: str, ttl: int) -> None:
        return None


class _RedisStore:
    """Upstash Redis over its REST API — no extra dependency, 2s timeout."""

    kind = "redis (shared)"

    def __init__(self, url: str, token: str) -> None:
        self._url = url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}

    async def _pipeline(self, commands):
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                r = await client.post(f"{self._url}/pipeline", headers=self._headers, json=commands)
                r.raise_for_status()
                out = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise StoreUnavailable(str(e)) from e
        if not isinstance(out, list) or any(isinstance(x, dict) and x.get("error") for x in out):
            raise StoreUnavailable(f"bad pipeline reply: {str(out)[:200]}")
        return [x.get("result") if isinstance(x, dict) else x for x in out]

    async def incr(self, key: str, by: int = 1, ttl: int = _DAY_TTL) -> int:
        res = await self._pipeline([["INCRBY", key, str(by)], ["EXPIRE", key, str(ttl)]])
        return int(res[0])

    async def get(self, key: str) -> int:
        res = await self._pipeline([["GET", key]])
        return int(res[0] or 0)

    async def get_str(self, key: str) -> Optional[str]:
        res = await self._pipeline([["GET", key]])
        return res[0]

    async def set_str(self, key: str, value: str, ttl: int) -> None:
        await self._pipeline([["SET", key, value, "EX", str(ttl)]])


_store = None


def get_store():
    return _get_store()


def _get_store():
    global _store
    if _store is None:
        if config.REDIS_REST_URL and config.REDIS_REST_TOKEN:
            _store = _RedisStore(config.REDIS_REST_URL, config.REDIS_REST_TOKEN)
        else:
            logger.warning(
                "AI spend limits are per instance: set UPSTASH_REDIS_REST_URL/TOKEN "
                "(or connect Upstash in Vercel) to enforce them across all instances."
            )
            _store = _MemoryStore()
    return _store


def _day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@dataclass
class Reservation:
    allowed: bool
    reason: str = ""          # why it was refused (machine-readable)
    message: str = ""         # why it was refused (for the user)


async def reserve(client_ip: Optional[str]) -> Reservation:
    """Ask for permission to make ONE AI call. Counts it if allowed."""
    if config.AI_MAX_CALLS_PER_DAY <= 0 or config.AI_MAX_SPEND_USD_PER_DAY <= 0:
        return Reservation(False, "ai_disabled", "AI search is turned off.")
    store = _get_store()
    day = _day()
    try:
        spent_usd = (await store.get(f"ai:{day}:spend_micro")) / 1e6
        if spent_usd + worst_case_call_cost() > config.AI_MAX_SPEND_USD_PER_DAY:
            return Reservation(False, "daily_spend_cap",
                               "Today's AI search budget is used up. Exact-figure checks still work.")
        calls = await store.incr(f"ai:{day}:calls")
        if calls > config.AI_MAX_CALLS_PER_DAY:
            return Reservation(False, "daily_call_cap",
                               "Today's AI search limit is reached. Exact-figure checks still work.")
        key = client_key(client_ip)
        if key:
            mine = await store.incr(f"ai:{day}:client:{key}")
            if mine > config.AI_MAX_CALLS_PER_CLIENT_PER_DAY:
                return Reservation(False, "per_client_cap",
                                   "You've reached today's AI search limit. Exact-figure checks still work.")
    except StoreUnavailable as e:
        logger.error("Spend guard store unavailable, refusing AI call: %s", e)
        return Reservation(False, "budget_store_unavailable",
                           "AI search is temporarily unavailable.")
    return Reservation(True)


# A dataset-pick call: no tools, a short prompt, a one-line answer.
PICK_MAX_INPUT_TOKENS = 4_000
PICK_MAX_OUTPUT_TOKENS = 200


def worst_case_pick_cost(model: Optional[str] = None) -> float:
    return estimate_cost(model or config.CLAUDE_SEARCH_MODEL, PICK_MAX_INPUT_TOKENS,
                         PICK_MAX_OUTPUT_TOKENS, 0)


async def reserve_pick(client_ip: Optional[str]) -> Reservation:
    """Permission for ONE dataset-pick call. Separate call caps (it is about
    fifty times cheaper than a web search); the same daily dollar cap."""
    if (config.AI_PICK_MAX_CALLS_PER_DAY <= 0 or config.AI_MAX_SPEND_USD_PER_DAY <= 0):
        return Reservation(False, "ai_disabled", "Dataset matching is turned off.")
    store = _get_store()
    day = _day()
    try:
        spent_usd = (await store.get(f"ai:{day}:spend_micro")) / 1e6
        if spent_usd + worst_case_pick_cost() > config.AI_MAX_SPEND_USD_PER_DAY:
            return Reservation(False, "daily_spend_cap", "Today's AI budget is used up.")
        calls = await store.incr(f"ai:{day}:pick_calls")
        if calls > config.AI_PICK_MAX_CALLS_PER_DAY:
            return Reservation(False, "daily_call_cap", "Today's dataset-matching limit is reached.")
        key = client_key(client_ip)
        if key:
            mine = await store.incr(f"ai:{day}:pick_client:{key}")
            if mine > config.AI_PICK_MAX_CALLS_PER_CLIENT_PER_DAY:
                return Reservation(False, "per_client_cap",
                                   "You've reached today's dataset-matching limit.")
    except StoreUnavailable as e:
        logger.error("Spend guard store unavailable, refusing pick call: %s", e)
        return Reservation(False, "budget_store_unavailable", "Dataset matching is temporarily unavailable.")
    return Reservation(True)


# Dataset finder: per round, the input is bounded by compact tool results
# (finder.py truncates them) and the output by FINDER_MAX_OUTPUT_TOKENS.
FINDER_MAX_INPUT_TOKENS_PER_TURN = 14_000
FINDER_MAX_OUTPUT_TOKENS = 700


def worst_case_finder_cost(model: Optional[str] = None) -> float:
    turns = config.AI_FINDER_MAX_TURNS
    return turns * estimate_cost(model or config.CLAUDE_SEARCH_MODEL,
                                 FINDER_MAX_INPUT_TOKENS_PER_TURN, FINDER_MAX_OUTPUT_TOKENS, 0)


async def reserve_finder(client_ip: Optional[str]) -> Reservation:
    """Permission for ONE dataset-finder run (all of its rounds). Its own call
    caps; the same daily dollar cap, checked against its worst case."""
    if config.AI_FINDER_MAX_CALLS_PER_DAY <= 0 or config.AI_MAX_SPEND_USD_PER_DAY <= 0:
        return Reservation(False, "ai_disabled", "The dataset finder is turned off.")
    store = _get_store()
    day = _day()
    try:
        spent_usd = (await store.get(f"ai:{day}:spend_micro")) / 1e6
        if spent_usd + worst_case_finder_cost() > config.AI_MAX_SPEND_USD_PER_DAY:
            return Reservation(False, "daily_spend_cap", "Today's AI budget is used up.")
        calls = await store.incr(f"ai:{day}:finder_calls")
        if calls > config.AI_FINDER_MAX_CALLS_PER_DAY:
            return Reservation(False, "daily_call_cap", "Today's dataset-search limit is reached.")
        key = client_key(client_ip)
        if key:
            mine = await store.incr(f"ai:{day}:finder_client:{key}")
            if mine > config.AI_FINDER_MAX_CALLS_PER_CLIENT_PER_DAY:
                return Reservation(False, "per_client_cap",
                                   "You've reached today's dataset-search limit.")
    except StoreUnavailable as e:
        logger.error("Spend guard store unavailable, refusing finder run: %s", e)
        return Reservation(False, "budget_store_unavailable",
                           "Dataset search is temporarily unavailable.")
    return Reservation(True)


async def record_spend(usd: float) -> None:
    if usd <= 0:
        return
    try:
        await _get_store().incr(f"ai:{_day()}:spend_micro", int(round(usd * 1e6)))
    except StoreUnavailable as e:
        logger.error("Could not record AI spend of $%.4f: %s", usd, e)


async def status() -> Dict[str, object]:
    store = _get_store()
    day = _day()
    out: Dict[str, object] = {
        "provider": config.AI_SEARCH_PROVIDER,
        "model": config.CLAUDE_SEARCH_MODEL if config.AI_SEARCH_PROVIDER == "claude" else None,
        "limit_store": store.kind,
        "max_calls_per_day": config.AI_MAX_CALLS_PER_DAY,
        "max_calls_per_client_per_day": config.AI_MAX_CALLS_PER_CLIENT_PER_DAY,
        "max_spend_usd_per_day": config.AI_MAX_SPEND_USD_PER_DAY,
        "worst_case_usd_per_call": round(worst_case_call_cost(), 4),
    }
    try:
        out["calls_today"] = await store.get(f"ai:{day}:calls")
        out["dataset_picks_today"] = await store.get(f"ai:{day}:pick_calls")
        out["spend_usd_today"] = round((await store.get(f"ai:{day}:spend_micro")) / 1e6, 4)
    except StoreUnavailable:
        out["limit_store"] = "unavailable (AI calls refused)"
    return out


def _reset_for_tests() -> None:
    global _store
    _store = None
