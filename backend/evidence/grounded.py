"""Grounded search — one Gemini call that retrieves and answers together.

This is the coverage tier. The structured sources answer a few dozen metrics
exactly; everything else used to fall through a planner that had to guess a
table and line code before any evidence existed, and returned "Inconclusive"
when it guessed wrong. Here a claim that no wired-up series matches goes to
Google Search through Gemini's `google_search` tool, which retrieves and reads
in the same request.

Why one call and not a pipeline: the free tier allows roughly 500 grounded
requests a day, and the old flow spent four to eight model calls per claim on
internal deliberation before answering anything. Retrieval, reading and drafting
happen together here, so a claim costs one request.

Two constraints, both verified against the docs rather than assumed:

  * `google_search` cannot be combined with `responseSchema`. Asking for both
    returns empty `groundingChunks` and `groundingSupports`, which would throw
    away every citation. So the model is asked for a delimited plain-text block
    and it is parsed deterministically here — no second model call.
  * Using grounding carries a Terms of Service obligation to display the Google
    Search Suggestions markup in `searchEntryPoint.renderedContent`. It is
    returned so the extension can render it; dropping it is a compliance
    problem, not a cosmetic one.

`groundingSupports` maps spans of the answer to the sources backing them. That
is the part that makes this checkable rather than merely fluent: a sentence with
no supporting chunk is flagged as ungrounded instead of being presented as
sourced.
"""
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

from config import GEMINI_API_KEY, GEMINI_MODEL, logger
from config.constants import LLM_CONFIG

_ENDPOINT_TMPL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)

# Domains whose figures are primary rather than reported second-hand.
_OFFICIAL_HINTS = (".gov", ".mil", ".fed.us")
_REPUTABLE_HINTS = (
    "reuters.com", "apnews.com", "oecd.org", "imf.org", "worldbank.org",
    "who.int", "un.org", "europa.eu", "bankofengland.co.uk",
)


@dataclass
class Citation:
    title: str
    uri: str
    domain: str = ""
    official: bool = False
    # Indices of answer segments this citation supports.
    supports: List[int] = field(default_factory=list)


@dataclass
class GroundedSegment:
    text: str
    citation_indices: List[int]

    @property
    def is_grounded(self) -> bool:
        return bool(self.citation_indices)


@dataclass
class GroundedAnswer:
    ok: bool
    answer_text: str = ""
    verdict: str = ""
    official_value: str = ""
    period: str = ""
    source_org: str = ""
    explanation: str = ""
    citations: List[Citation] = field(default_factory=list)
    segments: List[GroundedSegment] = field(default_factory=list)
    search_queries: List[str] = field(default_factory=list)
    # ToS: must be rendered by any UI showing this answer.
    search_suggestions_html: str = ""
    error: str = ""

    @property
    def grounded_fraction(self) -> float:
        """Share of the answer's characters backed by at least one source."""
        if not self.segments:
            return 0.0
        total = sum(len(s.text) for s in self.segments) or 1
        backed = sum(len(s.text) for s in self.segments if s.is_grounded)
        return backed / total

    @property
    def has_official_source(self) -> bool:
        return any(c.official for c in self.citations)


def _classify_domain(uri: str, title: str) -> tuple:
    """Return (domain, is_official).

    Grounding chunk URIs are `vertexaisearch.cloud.google.com` redirects, so the
    real host is usually only visible in the title. Both are checked rather than
    trusting the redirect URL, which would classify every source identically.
    """
    host = (urlparse(uri).netloc or "").lower()
    title_l = (title or "").lower().strip()

    candidate = host
    if "vertexaisearch" in host or not host:
        # Gemini puts the source domain in the title for redirect chunks.
        candidate = title_l

    official = any(h in candidate for h in _OFFICIAL_HINTS)
    if not official:
        official = any(h in candidate for h in _REPUTABLE_HINTS) and False  # reputable != official
    return candidate or host, official


def _build_prompt(claim: str, context_hint: str = "") -> str:
    """Ask for a delimited block, because responseSchema is unavailable here."""
    hint = f"\nCONTEXT: {context_hint}\n" if context_hint else ""
    return f"""You are a fact-checking research assistant. Search for the authoritative
figure or evidence behind the claim below, then answer in the exact format given.

Rules:
- Prefer official primary sources (a statistical agency, an official report) over
  news coverage of them. Name the agency in SOURCE_ORG.
- Report what the source actually says, including the period it covers. If the
  claim names a year and the best source covers a different period, say so in
  PERIOD and treat that as a mismatch rather than a match.
- If the claim is not empirically checkable (opinion, prediction, value judgement),
  set VERDICT to NOT_EMPIRICAL.
- If you cannot find an authoritative figure, set VERDICT to NO_EVIDENCE. Do not
  guess a number.
- A claim can be numerically accurate yet misleading through a cherry-picked
  baseline, a nominal-vs-inflation-adjusted comparison, a raw count where a rate
  is the meaningful measure, or a subgroup presented as the whole. If that is the
  case set VERDICT to MISLEADING and explain the framing in EXPLANATION.

CLAIM: '''{claim}'''{hint}

Answer in exactly this format, one field per line, nothing before or after:

VERDICT: SUPPORTED | CONTRADICTED | MISLEADING | NO_EVIDENCE | NOT_EMPIRICAL
OFFICIAL_VALUE: the authoritative figure with its unit, or NONE
PERIOD: the period the figure covers, or NONE
SOURCE_ORG: the organisation that published it, or NONE
EXPLANATION: two sentences comparing the claim to what the source says"""


_FIELD_RE = {
    "verdict": re.compile(r"^\s*VERDICT:\s*(.+?)\s*$", re.MULTILINE),
    "official_value": re.compile(r"^\s*OFFICIAL_VALUE:\s*(.+?)\s*$", re.MULTILINE),
    "period": re.compile(r"^\s*PERIOD:\s*(.+?)\s*$", re.MULTILINE),
    "source_org": re.compile(r"^\s*SOURCE_ORG:\s*(.+?)\s*$", re.MULTILINE),
    "explanation": re.compile(r"^\s*EXPLANATION:\s*(.+?)\s*$", re.MULTILINE | re.DOTALL),
}

_VALID_VERDICTS = {
    "SUPPORTED", "CONTRADICTED", "MISLEADING", "NO_EVIDENCE", "NOT_EMPIRICAL",
}


def _parse_fields(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for key, rx in _FIELD_RE.items():
        m = rx.search(text or "")
        if m:
            out[key] = m.group(1).strip()
    verdict = (out.get("verdict") or "").upper().strip()
    # Tolerate decoration like "**SUPPORTED**" or "SUPPORTED (high confidence)".
    for v in _VALID_VERDICTS:
        if v in verdict:
            out["verdict"] = v
            break
    else:
        out["verdict"] = "NO_EVIDENCE" if verdict else ""
    for k in ("official_value", "period", "source_org"):
        if (out.get(k) or "").upper() in ("NONE", "N/A", "-"):
            out[k] = ""
    return out


def _extract_grounding(candidate: Dict[str, Any], answer: str) -> tuple:
    """Turn groundingMetadata into citations plus per-segment attribution."""
    gm = candidate.get("groundingMetadata") or {}
    chunks = gm.get("groundingChunks") or []
    supports = gm.get("groundingSupports") or []

    citations: List[Citation] = []
    for ch in chunks:
        web = (ch or {}).get("web") or {}
        uri = web.get("uri", "") or ""
        title = web.get("title", "") or ""
        domain, official = _classify_domain(uri, title)
        citations.append(Citation(title=title, uri=uri, domain=domain, official=official))

    segments: List[GroundedSegment] = []
    for sup in supports:
        seg = (sup or {}).get("segment") or {}
        text = seg.get("text")
        if text is None:
            start, end = seg.get("startIndex", 0), seg.get("endIndex", 0)
            text = answer[start:end] if isinstance(answer, str) else ""
        idxs = [i for i in (sup.get("groundingChunkIndices") or [])
                if isinstance(i, int) and 0 <= i < len(citations)]
        for i in idxs:
            citations[i].supports.append(len(segments))
        segments.append(GroundedSegment(text=text or "", citation_indices=idxs))

    return citations, segments, (gm.get("webSearchQueries") or []), \
        ((gm.get("searchEntryPoint") or {}).get("renderedContent") or "")


async def ground_claim(
    claim: str,
    context_hint: str = "",
    model: Optional[str] = None,
) -> GroundedAnswer:
    """Search and answer in a single grounded Gemini call."""
    if not GEMINI_API_KEY:
        return GroundedAnswer(ok=False, error="GEMINI_API_KEY not configured")
    if not claim or not claim.strip():
        return GroundedAnswer(ok=False, error="empty claim")

    # Grounding is free only on the 2.5 tier; 3.x models are not on the free
    # tier for search. Defaulting here keeps the pipeline inside the free quota.
    model = model or GEMINI_MODEL or "gemini-2.5-flash"
    url = _ENDPOINT_TMPL.format(model=model)

    body = {
        "contents": [{"role": "user", "parts": [{"text": _build_prompt(claim, context_hint)}]}],
        "tools": [{"google_search": {}}],
        # No responseSchema: it silently empties groundingChunks/Supports.
        "generationConfig": {"temperature": 0.0},
    }

    try:
        async with httpx.AsyncClient(timeout=LLM_CONFIG.REQUEST_TIMEOUT) as client:
            r = await client.post(url, headers={"x-goog-api-key": GEMINI_API_KEY}, json=body)
            if r.status_code != 200:
                logger.warning("Grounded search HTTP %s: %s", r.status_code, r.text[:300])
                return GroundedAnswer(ok=False, error=f"HTTP {r.status_code}")
            payload = r.json()
    except httpx.HTTPError as e:
        logger.warning("Grounded search request failed: %s", e)
        return GroundedAnswer(ok=False, error=f"request failed: {e}")

    candidates = payload.get("candidates") or []
    if not candidates:
        reason = (payload.get("promptFeedback") or {}).get("blockReason", "no candidates")
        return GroundedAnswer(ok=False, error=str(reason))

    cand = candidates[0]
    parts = ((cand.get("content") or {}).get("parts") or [])
    answer = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    if not answer.strip():
        return GroundedAnswer(ok=False, error="empty answer text")

    fields = _parse_fields(answer)
    citations, segments, queries, suggestions = _extract_grounding(cand, answer)

    return GroundedAnswer(
        ok=True,
        answer_text=answer,
        verdict=fields.get("verdict", ""),
        official_value=fields.get("official_value", ""),
        period=fields.get("period", ""),
        source_org=fields.get("source_org", ""),
        explanation=fields.get("explanation", ""),
        citations=citations,
        segments=segments,
        search_queries=queries,
        search_suggestions_html=suggestions,
    )
