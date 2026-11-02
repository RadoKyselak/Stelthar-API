"""The single entry point for the paid AI search tier.

Order of checks, cheapest first — nothing that costs money runs until every
free check has passed:

  1. Provider switched off            -> no call
  2. Input gate (length, word count, must name a figure or an official
     statistic)                       -> no call
  3. Answer cache (shared; a week for a past year, else 24h) -> no call
  4. Spend guard (daily $ cap incl. worst case of this call, daily call cap,
     per-client cap)                  -> no call
  5. One capped provider call
"""
import hashlib
import json
import re
from dataclasses import asdict
from typing import Optional

import config
from config import logger
from evidence import cache as disk_cache
from evidence.grounded import Citation, GroundedAnswer, GroundedSegment
from evidence.grounded import ground_claim as gemini_ground_claim
from utils import spend_guard

_CACHE_TTL = 24 * 3600
_MAX_WORDS = 60
_DIGIT_RE = re.compile(r"\d")
_WORD_RE = re.compile(r"[A-Za-z]{2,}")
# Judgement words in a claim with no number: an opinion, declined before any
# spend. A claim with a figure is checked whatever adjectives surround it.
_OPINION_RE = re.compile(
    r"\b(?:best|worst|greatest|out of control|should|shouldn't|ought to|deserves?|"
    r"overrated|underrated|terrible|awful|horrible|amazing|wonderful|beautiful|ugly|"
    r"stupid|dumb|evil|disgrace(?:ful)?|disaster|i think|i believe|i feel|in my (?:opinion|view)|"
    r"favou?rite|tastes?|delicious|boring|exciting|corrupt|incompetent|genius|"
    r"the (?:only|real) (?:reason|problem))\b", re.I)
# "Is the unemployment rate 4%?" asks about a statement, and is checked as one.
_YES_NO_RE = re.compile(r"^\s*(?:is|are|was|were|did|does|do|has|have|had|can|could|will|"
                        r"would|should|isn't|aren't|wasn't|weren't|didn't|doesn't|don't|"
                        r"hasn't|haven't)\b(?!\s+(?:you|claude)\b)", re.I)
# A claim is a statement about the world. Text that instructs the model
# ("ignore…", "write…", "pretend…") is not a claim, whatever numbers it
# contains, and is declined before anything is spent.
_INSTRUCTION_RE = re.compile(
    r"^\s*(?:please\s+)?(?:ignore|disregard|forget|write|compose|generate|create|draft|"
    r"translate|summari[sz]e|explain|tell me|give me|list|pretend|act as|roleplay|"
    r"role-play|you are|you're|imagine|repeat|print|output|reveal|show me|describe|"
    r"help me|can you|could you|would you)\b"
    r"|\b(?:ignore|disregard)\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|earlier)\b"
    r"|\bsystem prompt\b|\byour instructions\b",
    re.IGNORECASE,
)


def gate(claim: str, names_a_metric: bool) -> Optional[str]:
    """Why this claim must not reach the paid tier, or None if it may."""
    text = (claim or "").strip()
    if len(text) > config.AI_MAX_CLAIM_CHARS:
        return (f"The claim is too long to check ({len(text)} characters; the limit is "
                f"{config.AI_MAX_CLAIM_CHARS}). Highlight a single sentence with one figure.")
    if len(text.split()) > _MAX_WORDS:
        return "The claim is too long to check. Highlight a single sentence with one figure."
    if _INSTRUCTION_RE.search(text) or (text.endswith("?") and not _YES_NO_RE.match(text)):
        return ("That reads as a question or an instruction, not a claim. Highlight a "
                "statement of fact, e.g. \u201cThe unemployment rate was 3.6% in 2023.\u201d")
    # A claim with no number ("Vaccines cause autism", "The CHIPS Act was signed
    # in August") is still checkable against the record. It needs a few words to
    # be a statement at all; an opinion comes back as not checkable without a search.
    if not _DIGIT_RE.search(text) and not names_a_metric:
        if len(_WORD_RE.findall(text)) < 3:
            return "That is too short to be a claim that can be checked."
        if _OPINION_RE.search(text):
            return ("That reads as an opinion, with no figure or fact that can be checked "
                    "against published data.")
    return None


_QUOTES = str.maketrans({"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'",
                         "\u2013": "-", "\u2014": "-", "\u00a0": " "})
_YEAR_RE = re.compile(r"\b(19[0-9]{2}|20[0-9]{2})\b")
_NOW_RE = re.compile(r"\b(now|today|currently|current|this year|this month|so far|latest)\b", re.I)
_WEEK = 7 * 24 * 3600


def normalise(claim: str) -> str:
    """The same sentence copied from two pages: curly or straight quotes, a
    trailing period or not, surrounding quote marks. One answer serves both."""
    t = " ".join((claim or "").translate(_QUOTES).lower().split())
    return t.strip(" \"'").rstrip(".;:!").strip(" \"'")


def ttl_for(claim: str) -> int:
    """A week for a claim about a closed past year (its answer will not
    change), a day for anything that may be about the present."""
    from datetime import datetime, timezone
    years = [int(y) for y in _YEAR_RE.findall(claim or "")]
    if years and max(years) < datetime.now(timezone.utc).year and not _NOW_RE.search(claim or ""):
        return _WEEK
    return _CACHE_TTL


def _cache_key(claim: str, hint: str) -> str:
    # Bumped when the prompt changes: "v4": a second search for a reliable page.
    return hashlib.sha256(f"v5|{config.AI_SEARCH_PROVIDER}|{normalise(claim)}|{hint}".encode()).hexdigest()


def _from_dict(d: dict) -> GroundedAnswer:
    d = dict(d)
    d["citations"] = [Citation(**c) for c in d.get("citations") or []]
    d["segments"] = [GroundedSegment(**s) for s in d.get("segments") or []]
    return GroundedAnswer(**d)


async def _cache_get(key: str, ttl: int = _CACHE_TTL) -> Optional[GroundedAnswer]:
    try:
        raw = await spend_guard.get_store().get_str(f"ai:answer:{key}")
        data = json.loads(raw) if raw else disk_cache.read("ai_answers", key, ttl)
        if data:
            g = _from_dict(data)
            g.model_calls, g.cost_usd = 0, 0.0
            return g
    except Exception as e:  # a cache problem must never cost money or fail a request
        logger.debug("AI answer cache read failed: %s", e)
    return None


async def _cache_put(key: str, g: GroundedAnswer, ttl: int = _CACHE_TTL) -> None:
    data = asdict(g)
    try:
        await spend_guard.get_store().set_str(f"ai:answer:{key}", json.dumps(data), ttl)
    except Exception as e:
        logger.debug("AI answer cache write (shared) failed: %s", e)
    disk_cache.write("ai_answers", key, data)


async def search(claim: str, hint: str = "", *, client_ip: Optional[str] = None,
                 names_a_metric: bool = False,
                 leads: Optional[list] = None) -> GroundedAnswer:
    provider = config.AI_SEARCH_PROVIDER
    if provider not in ("claude", "gemini"):
        return GroundedAnswer(ok=False, error="Web search is turned off.", model_calls=0, skipped=True)

    why_not = gate(claim, names_a_metric)
    if why_not:
        return GroundedAnswer(ok=False, error=why_not, model_calls=0, skipped=True)

    key, ttl = _cache_key(claim, hint), ttl_for(claim)
    hit = await _cache_get(key, ttl)
    if hit is not None:
        return hit

    res = await spend_guard.reserve(client_ip)
    if not res.allowed:
        logger.warning("AI call refused by spend guard: %s", res.reason)
        return GroundedAnswer(ok=False, error=res.message, model_calls=0,
                              skipped=True, limited=True)

    if provider == "claude":
        from evidence import claude_search
        g = await claude_search.ground_claim(claim, context_hint=hint, known_pages=leads or [])
    else:
        g = await gemini_ground_claim(claim, context_hint=hint)

    g.leads = list(leads or [])
    if g.ok:
        await _cache_put(key, g, ttl)
    return g
