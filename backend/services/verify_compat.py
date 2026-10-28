"""`/verify`, answered by the v2 engine in the v1 response shape.

Every installed copy of the extension — including the Chrome Web Store build,
which cannot be updated without a review cycle — posts to `/verify`. That path
ran the legacy multi-call planner, so when the Gemini model it depended on was
retired, every user got "Inconclusive" for every claim.

Rather than wait for a store update, `/verify` now runs the same engine as
`/v2/verify` (exact figures from BLS, BEA, Census and Treasury at zero model
calls, grounded search otherwise) and translates the result into the fields
the old popup reads: `verdict`, numeric `confidence`, `confidence_tier`,
`summary`, `evidence_links` and `sources`. The v2 fields ride along unchanged
so updated clients can render the richer result.

The legacy planner remains reachable at `/v1/verify` for comparison.
"""
from typing import Any, Dict, List

from claims.verdict import NO_VERDICT
from services.verify_v2 import TIER_GROUNDED, VerificationV2, to_response_dict
from services.verify_v2 import verify as verify_v2

# The old popup prints confidence as a percentage. A tier is the honest
# signal; these numbers only exist so that display keeps working.
_TIER_TO_SCORE = {"High": 0.9, "Medium": 0.7, "Low": 0.4}

_DEGRADED_REASONS = {"source_unavailable"}


def _legacy_verdict(v: VerificationV2) -> str:
    # The old UI styles exactly three verdicts. "Misleading" renders as plain
    # text there, which is acceptable; "No verdict" maps to the closest one.
    return "Inconclusive" if v.verdict == NO_VERDICT else v.verdict


def _summary(v: VerificationV2) -> str:
    parts = [v.headline.strip()] if v.headline else []
    for c in v.caveats:
        c = (c or "").strip()
        if c and c not in parts:
            parts.append(f"Note: {c}")
    return " ".join(parts) or "No summary available."


def _evidence_links(v: VerificationV2) -> List[Dict[str, str]]:
    links: List[Dict[str, str]] = []
    for e in v.evidence:
        if not e.get("source_url"):
            continue
        finding = f"{e.get('label')}: {e.get('value_display')} ({e.get('period')})"
        if e.get("geography") and e.get("geography") != "United States":
            finding += f", {e['geography']}"
        links.append({"finding": f"{finding} — {e.get('source')}", "source_url": e["source_url"]})
    for c in v.citations:
        if c.get("url"):
            label = c.get("title") or c.get("domain") or c["url"]
            if c.get("is_official_source"):
                label += " (official source)"
            links.append({"finding": label, "source_url": c["url"]})
    return links


def _sources(v: VerificationV2) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for e in v.evidence:
        series = f" {e['series_id']}" if e.get("series_id") else ""
        out.append({
            "title": f"{e.get('source')}{series} — {e.get('label')}",
            "url": e.get("source_url"),
            "snippet": f"{e.get('value_display')} for {e.get('period')}, {e.get('geography')}",
            "data_value": e.get("value"),
            "unit": e.get("unit"),
        })
    for c in v.citations:
        out.append({
            "title": c.get("title") or c.get("domain") or "Source",
            "url": c.get("url"),
            "snippet": c.get("domain") or "",
            "data_value": None,
            "unit": None,
        })
    return out


def to_legacy_shape(v: VerificationV2) -> Dict[str, Any]:
    """v1 field names first, v2 fields alongside."""
    degraded = v.reason in _DEGRADED_REASONS or any(
        n.startswith("degraded") for n in v.notes
    )
    # Grounding failed outright (model or quota problem): not a judgment.
    if v.verdict == NO_VERDICT and v.tier == "none" and v.model_calls > 0 \
            and v.headline.startswith("Could not retrieve evidence"):
        degraded = True

    confidence = 0.0 if v.verdict == NO_VERDICT else _TIER_TO_SCORE.get(v.confidence, 0.4)
    v2 = to_response_dict(v)

    out: Dict[str, Any] = {
        # ---- v1 contract -------------------------------------------------
        "claim_original": v.claim,
        "claim_normalized": v.claim,
        "claim_type": (v.parsed or {}).get("metric") or "other",
        "degraded": degraded,
        "degraded_reason": (v.reason or "llm_unavailable") if degraded else None,
        "verdict": _legacy_verdict(v),
        "confidence": confidence,
        "confidence_tier": v.confidence,
        "summary": _summary(v),
        "evidence_links": _evidence_links(v),
        "sources": _sources(v),
        "debug_plan": {"parsed": v.parsed, "tier": v.tier},
        "debug_log": [],
        "debug_iterations": [],
        "debug_notes": list(v.notes),
        # ---- v2 detail, for clients that understand it --------------------
        "engine": "v2",
        "verdict_detail": v.verdict,
    }
    for k, val in v2.items():
        if k in ("verdict", "confidence", "claim"):
            continue
        out.setdefault(k, val)
    if v.tier != TIER_GROUNDED:
        out["search_suggestions_html"] = None
    return out


async def verify(claim: str) -> Dict[str, Any]:
    return to_legacy_shape(await verify_v2(claim))
