"""Check every claim in a passage, each on its own.

`/v2/verify` answers one claim. This answers a paragraph, a speech or the
figures on a page: the text is cut into its claims (claims/splitter.py), each
goes through the same engine, and the answers come back in order with a count
of how they fell.

What it costs is decided here, and it is nothing:

  * One claim in the text: the full pipeline, exactly as `/v2/verify` runs it,
    so a single highlighted sentence behaves as it always has.
  * Two or more: free government data only. No dataset picker, no interpreter,
    no web search. A passage with ten claims the rules cannot read would
    otherwise spend ten paid calls, and a visitor's whole daily allowance, on
    one click. A claim the free data cannot settle comes back as "Can't verify"
    with `deeper` set, and the client offers the full check for that one claim
    on request. The person asks for the paid step; it never runs by itself.

Nothing here reaches a verdict. Each claim's answer is the engine's own.
"""
import asyncio
import time
from typing import Any, Dict, List, Optional

import config
from claims import splitter
from claims.verdict import LOW, NO_SERIES, NO_VERDICT, NOT_EMPIRICAL, PERIOD_UNAVAILABLE
from config import logger
from services.verify_v2 import VerificationV2, to_response_dict, verify

TEXT = "text"   # something a person pasted or highlighted: every claim is listed
PAGE = "page"   # a page scan: only what the free data can settle

# How a tally names each outcome; the same keys the display block uses.
_TALLY_KEYS = ("supported", "mostly-accurate", "contradicted", "misleading", "no-verdict", "unavailable")
# Reasons the full pipeline cannot improve on: the period does not exist yet,
# the claim is an opinion, or a source is down.
_FINAL_REASONS = frozenset({PERIOD_UNAVAILABLE, NOT_EMPIRICAL, "source_unavailable"})


def _could_go_deeper(v: VerificationV2) -> bool:
    """Whether the full check (dataset search, then the web) might settle what
    the free data could not."""
    return v.verdict == NO_VERDICT and v.reason not in _FINAL_REASONS


def _entry(index: int, span: splitter.ClaimSpan, v: VerificationV2, deeper: bool) -> Dict[str, Any]:
    return {
        "index": index,
        "span": span.span,
        "start": span.start,
        "end": span.end,
        "claim": span.claim,
        "kind": span.kind,
        "inherited": list(span.inherited),
        "deeper": deeper,
        "result": to_response_dict(v),
    }


def _tally(entries: List[Dict[str, Any]]) -> Dict[str, int]:
    tally = {k: 0 for k in _TALLY_KEYS}
    for e in entries:
        key = e["result"]["display"]["verdict"]["key"]
        tally[key if key in tally else "no-verdict"] += 1
    return tally


async def check_text(text: str, mode: str = TEXT, client_ip: Optional[str] = None,
                     free_only: bool = False) -> Dict[str, Any]:
    """Every claim in `text`, checked. Never raises. `free_only` keeps even a
    single claim on agency data: for callers that are not a person asking
    (an assistant's tool call), who must never spend a paid call."""
    started = time.monotonic()
    text = (text or "").strip()
    page = mode == PAGE
    spans: List[splitter.ClaimSpan] = []
    truncated = False
    if config.MULTI_CLAIM:
        kinds = (splitter.STATISTIC, splitter.RECORD) if page else (
            splitter.STATISTIC, splitter.RECORD, splitter.OTHER)
        try:
            spans, truncated = splitter.split_claims(text, config.CHECK_MAX_CLAIMS, kinds)
        except Exception:  # noqa: BLE001 — a splitter bug must not take the check down
            logger.exception("Splitting failed for %r", text[:120])
            spans, truncated = [], False

    if page and not spans:
        entries: List[Dict[str, Any]] = []
    elif len(spans) <= 1 and not page:
        # One claim: the whole text, through the whole pipeline. The engine
        # picks the sentence itself when the text is long, as it did before.
        whole = splitter.ClaimSpan(text, text, 0, len(text), spans[0].kind if spans else splitter.OTHER)
        if free_only:
            v = await verify(text, allow_grounding=False, client_ip=client_ip)
            entries = [_entry(0, whole, v, _could_go_deeper(v))]
        else:
            from evidence import fact_checks
            related = fact_checks.start(text)
            v = await verify(text, client_ip=client_ip)
            v.fact_checks = await fact_checks.finish(related)
            entries = [_entry(0, whole, v, False)]
    else:
        gate = asyncio.Semaphore(config.CHECK_CONCURRENCY)

        async def one(span: splitter.ClaimSpan) -> VerificationV2:
            if span.kind == splitter.OTHER:
                # A figure, but no measure the free data covers was read from
                # it with confidence. The engine is not asked: whatever it
                # made of the sentence would be a guess with a verdict on it.
                return VerificationV2(
                    claim=span.claim, verdict=NO_VERDICT, confidence=LOW, reason=NO_SERIES,
                    headline="None of the official data series we check covers this claim.")
            async with gate:
                return await verify(span.claim, allow_grounding=False, client_ip=client_ip)

        results = await asyncio.gather(*(one(s) for s in spans))
        entries = [_entry(i, s, v, _could_go_deeper(v)) for i, (s, v) in enumerate(zip(spans, results))]

    return {
        "mode": PAGE if page else TEXT,
        "claims": entries,
        "count": len(entries),
        "tally": _tally(entries),
        "truncated": truncated,
        "model_calls": sum(e["result"].get("model_calls") or 0 for e in entries),
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }
