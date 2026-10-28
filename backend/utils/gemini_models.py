"""Pick a Gemini model that still exists, and keep using it.

Production broke because the model name was a constant: Google retired
`gemini-2.5-flash` for this key, every generateContent call returned 404, and
the whole service degraded to "Inconclusive" with nothing louder than a log
line. A single hard-coded model is a time bomb with a vendor-controlled fuse.

So every call goes through `generate()`, which walks an ordered candidate list:

    GEMINI_MODEL (env)  ->  GEMINI_FALLBACK_MODELS (env)  ->  built-in defaults

A model that answers "not found / no longer available" is marked dead for the
life of the process and the next one is tried in the same request. The first
model that answers is remembered, so the cost of a retirement is one extra
round trip per cold start rather than an outage.

`gemini-flash-latest` is last on purpose: it is Google's moving alias, so it
survives retirements, but its behaviour can change underneath us. Pinned
models come first; the alias is the safety net.
"""
import copy
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from config import GEMINI_FALLBACK_MODELS, GEMINI_MODEL, logger

GENERATE_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

_working: Optional[str] = None
_dead: set = set()
# Models that answered 429 recently. Quotas are per model, so the next
# candidate may still have headroom; the throttled one is retried after the
# cooldown rather than written off.
_cooldown: Dict[str, float] = {}
QUOTA_COOLDOWN_SECONDS = 60.0

_GONE_MARKERS = (
    "no longer available", "not found", "is not supported", "was not found",
    "has been deprecated", "is deprecated", "unknown model",
)


def candidates(preferred: Optional[str] = None) -> List[str]:
    """Models to try, best first, skipping any known to be gone."""
    order: List[str] = []
    for m in (preferred, _working, GEMINI_MODEL, *GEMINI_FALLBACK_MODELS):
        m = (m or "").strip()
        if m and m not in order and m not in _dead:
            order.append(m)
    now = time.monotonic()
    fresh = [m for m in order if _cooldown.get(m, 0.0) <= now]
    # Throttled models go last, not away: if every model is throttled, still
    # try them rather than failing without a request.
    order = fresh + [m for m in order if m not in fresh]
    if not order:
        # Everything is marked dead. Retry the alias rather than refusing
        # outright — the dead set may be stale after a transient 404.
        order = [GEMINI_FALLBACK_MODELS[-1] if GEMINI_FALLBACK_MODELS else "gemini-flash-latest"]
    return order


def active_model() -> str:
    """The model calls will go to next — for /health and logs."""
    return candidates()[0]


def _is_model_gone(response: httpx.Response) -> bool:
    status = getattr(response, "status_code", 200)
    if not isinstance(status, int) or status not in (400, 404):
        return False
    try:
        text = (response.text or "").lower()
    except Exception:
        text = ""
    if status == 404:
        return True
    return "model" in text and any(m in text for m in _GONE_MARKERS)


def _is_gen3_or_later(model: str) -> bool:
    m = model.lower()
    return m.startswith("gemini-3") or m.startswith("gemini-4") or "latest" in m


def tune_body(body: Dict[str, Any], model: str) -> Dict[str, Any]:
    """Adjust generationConfig for the model family.

    Gemini 3+ is tuned for the default temperature (Google's guidance is that
    lowering it can cause looping), and its default "high" thinking adds
    seconds a fact-check lookup does not need. 2.x keeps the caller's config.
    """
    if not _is_gen3_or_later(model):
        return body
    out = copy.deepcopy(body)
    gen = dict(out.get("generationConfig") or {})
    gen.pop("temperature", None)
    gen.setdefault("thinkingConfig", {"thinkingLevel": "low"})
    out["generationConfig"] = gen
    return out


def _without_thinking(body: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(body)
    gen = dict(out.get("generationConfig") or {})
    gen.pop("thinkingConfig", None)
    out["generationConfig"] = gen
    return out


async def generate(
    client: httpx.AsyncClient,
    body: Dict[str, Any],
    headers: Dict[str, str],
    preferred: Optional[str] = None,
) -> Tuple[httpx.Response, str]:
    """POST generateContent, falling through retired models.

    Returns (response, model_used). The response may still be an error — only
    "this model does not exist" is handled here; quota and server errors are
    the caller's to interpret.
    """
    global _working
    last: Optional[httpx.Response] = None
    model = ""
    for model in candidates(preferred):
        payload = tune_body(body, model)
        r = await client.post(GENERATE_URL.format(model=model), headers=headers, json=payload)
        if (
            getattr(r, "status_code", 200) == 400
            and "thinking" in (getattr(r, "text", "") or "").lower()
            and "thinkingConfig" in (payload.get("generationConfig") or {})
        ):
            # The model exists but rejects the thinking knob; drop it once.
            r = await client.post(
                GENERATE_URL.format(model=model), headers=headers,
                json=_without_thinking(payload),
            )
        if getattr(r, "status_code", None) == 429:
            logger.warning(
                "Gemini model %s is over quota (HTTP 429): %s",
                model, (getattr(r, "text", "") or "")[:1500],
            )
            _cooldown[model] = time.monotonic() + QUOTA_COOLDOWN_SECONDS
            last = r
            continue
        if _is_model_gone(r):
            logger.error(
                "Gemini model %s is unavailable (HTTP %s); trying the next candidate",
                model, r.status_code,
            )
            _dead.add(model)
            last = r
            continue
        if isinstance(getattr(r, "status_code", None), int) and r.status_code < 400:
            if _working != model:
                logger.info("Gemini model in use: %s", model)
            _working = model
        return r, model
    return last, model


def response_text(payload: Dict[str, Any]) -> str:
    """Join the answer's text parts, skipping thought summaries.

    Gemini 3 can return several parts (and thought parts when asked), so
    reading only parts[0] — what the old client did — can return a fragment.
    """
    try:
        cands = payload.get("candidates") or []
        if not cands:
            return ""
        parts = ((cands[0].get("content") or {}).get("parts") or [])
        return "".join(
            p.get("text", "") for p in parts
            if isinstance(p, dict) and not p.get("thought")
        )
    except (AttributeError, TypeError):
        return ""


def _reset_for_tests() -> None:
    global _working
    _working = None
    _dead.clear()
    _cooldown.clear()
