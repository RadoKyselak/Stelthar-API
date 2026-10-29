"""Plain-language presentation of a verification result.

Every client (the extension's in-page panel, its popup, the web page) renders
this one `display` block. The wording therefore lives in one place, and it
improves for every installed copy as soon as the API deploys, with no Chrome
Web Store review in between.

Nothing here changes a verdict. It only explains one: what the verdict means,
which figure it rests on and where that figure came from, how the check was
done, how to cite it, and what to try when there is no answer.
"""
from datetime import date
from html import escape
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from claims import verdict as V
from evidence.citation import (
    _READABLE_PAGES, agency_name, agency_short, clean_basis, dataset_name, identifier,
)

# key, meaning
_VERDICTS = {
    V.SUPPORTED: ("supported", "Matches the official figure."),
    V.MOSTLY_ACCURATE: ("mostly-accurate", "Close to the official figure, but not exact."),
    V.CONTRADICTED: ("contradicted", "The official figure says otherwise."),
    V.MISLEADING: ("misleading", "The number may be right, but the framing misleads."),
    V.NO_VERDICT: ("no-verdict", "No official figure settles this claim."),
}
# What the card says, where the API's verdict word would read as jargon.
_LABELS = {V.NO_VERDICT: "Can't verify"}

# why, what to try
_REASONS = {
    V.NO_SERIES: (
        "None of the official data series we check covers this claim.",
        "Claims with a national or state figure work best: unemployment, inflation, "
        "GDP, prices, the deficit, the national debt, population or income.",
    ),
    V.PERIOD_UNAVAILABLE: (
        "The agency has not published a figure for that period yet.",
        "Try an earlier period, or check again after the next release.",
    ),
    V.PERIOD_MISMATCH: (
        "The official figure we found covers a different period than the claim.",
        "Name the exact year, quarter or month in the claim.",
    ),
    V.GEOGRAPHY_UNSUPPORTED: (
        "Official figures are not published for that place at this level.",
        "Try the state or national figure.",
    ),
    V.NOT_EMPIRICAL: (
        "This is an opinion or a prediction, not a statement that data can check.",
        "",
    ),
    V.NO_VALUE_IN_CLAIM: (
        "The claim does not state a number to check.",
        "The official figure is shown above.",
    ),
    V.TREND_CLAIM: (
        "The claim compares points in time, and one figure cannot settle that.",
        "Give each figure its own year, e.g. “from 8.1% in 2020 to 3.6% in 2023”.",
    ),
    V.UNRESOLVED_PERIOD: (
        "The claim's time frame cannot be pinned to a date.",
        "Name the year instead of a relative period such as “when X took office”.",
    ),
    V.ALTERNATE_MEASURE: (
        "The agency publishes more than one official figure for this, and the claim "
        "may be quoting the other one.",
        "",
    ),
    "no_source_found": (
        "No published source could be found to check this against.",
        "",
    ),
    "source_unavailable": (
        "A government data source did not respond.",
        "Try again in a minute.",
    ),
}

_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")


def _safe_url(url: Optional[str]) -> str:
    """Only http(s) links reach a client; web-search results are untrusted."""
    if not url:
        return ""
    try:
        scheme = urlparse(url).scheme.lower()
    except ValueError:
        return ""
    return url if scheme in ("http", "https") else ""


def _long_date(iso: str) -> str:
    try:
        d = date.fromisoformat(iso[:10])
    except ValueError:
        return iso
    return f"{_MONTHS[d.month - 1]} {d.day}, {d.year}"


def _period_text(e: Dict[str, Any]) -> str:
    if e.get("period_kind") == "point_in_time" and e.get("as_of"):
        return _long_date(e["as_of"])
    return e.get("period_label") or e.get("period") or ""


def _data_source(e: Dict[str, Any]) -> Dict[str, Any]:
    src = e.get("source") or ""
    sid = e.get("series_id") or ""
    basis = clean_basis(e.get("period_basis") or "")
    data_url = _safe_url(e.get("source_url"))
    url = _READABLE_PAGES.get(sid, data_url)
    place = e.get("geography") or ""
    publisher = e.get("publisher") or ""
    return {
        "kind": "data",
        "official": True,
        "agency": f"{publisher} (via FRED)" if src.upper() == "FRED" and publisher else agency_name(src),
        "agency_short": agency_short(src),
        "dataset": dataset_name(src, sid, basis),
        "identifier": identifier(src, sid, table=e.get("table"), as_of=e.get("as_of")),
        "measure": e.get("label") or "",
        "value": e.get("value_display") or "",
        "period": _period_text(e),
        "basis": basis,
        "place": "" if place == "United States" else place,
        "url": url,
        "data_url": data_url if data_url != url else "",
    }


def _web_source(c: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    url = _safe_url(c.get("url"))
    if not url:
        return None
    domain = (c.get("domain") or urlparse(url).netloc or "").lower()
    if domain.startswith("www."):
        domain = domain[4:]
    return {
        "kind": "web",
        "official": bool(c.get("is_official_source")),
        "title": (c.get("title") or domain)[:200],
        "domain": domain,
        "url": url,
    }


def _note(p: Dict[str, Any]) -> str:
    """One line on how the two figures relate, in words a reader can check."""
    verdict, gap, comparator = p.get("verdict"), p.get("gap") or "", p.get("comparator")
    official, claim = p.get("official") or "", p.get("claim") or ""
    if not claim:
        return ""
    if comparator != "equals":
        # "$6.75 trillion is not more than $7 trillion."
        return f"{official} is {'' if verdict == V.SUPPORTED else 'not '}{claim}."
    if verdict == V.SUPPORTED:
        return f"Off by {gap}, within the claim's margin." if gap else "Exact match."
    return f"Off by {gap}." if gap else ""


def _short_basis(basis: str) -> str:
    """What fits under a number: "annual average", "12-month change from
    June 2021". The full basis is always in the citation."""
    head = (basis or "").split(",")[0].strip()
    return head if len(head) <= 34 else ""


def _relation(p: Dict[str, Any]) -> str:
    """exact | within (rounds to the claim) | off | meets | fails (thresholds)."""
    if not p.get("claim"):
        return ""
    if p.get("comparator") != "equals":
        return "meets" if p.get("verdict") == V.SUPPORTED else "fails"
    if not p.get("gap"):
        return "exact"
    return "within" if p.get("verdict") == V.SUPPORTED else "off"


def _gap_short(gap: str) -> str:
    """"0.1 percentage point" -> "0.1 pt"; "$32.82 billion" stays."""
    return (gap or "").replace(" percentage points", " pts").replace(" percentage point", " pt")


def _comparison(p: Dict[str, Any]) -> Dict[str, Any]:
    place = p.get("place") or ""
    basis = p.get("basis") or ""
    when = p.get("period") or ""
    context = " · ".join(x for x in (
        "" if place == "United States" else place, when, _short_basis(basis),
    ) if x)
    return {
        "label": p.get("label"),
        "verdict_key": _VERDICTS.get(p.get("verdict"), ("no-verdict", ""))[0],
        "claim": p.get("claim") or "",
        "official": p.get("official") or "",
        "source": agency_short(p.get("source") or ""),
        "context": context,
        "note": _note(p),
        "measure": p.get("measure") or "",
        "place": "" if place == "United States" else place,
        "period": when,
        "basis": _short_basis(basis),
        "relation": _relation(p),
        "gap": p.get("gap") or "",
        "gap_short": _gap_short(p.get("gap") or ""),
        "citation": p.get("citation") or "",
        "citation_html": p.get("citation_html") or "",
        "url": _safe_url(p.get("url")),
    }


def _status(v) -> str:
    """"ok", "unavailable" (an outage on our side) or "limited" (a spend cap)."""
    notes = v.notes or []
    if any(n.startswith("degraded: ai limit") for n in notes):
        return "limited"
    if v.reason == "source_unavailable" or any(n.startswith("degraded") for n in notes):
        return "unavailable"
    if (v.verdict == V.NO_VERDICT and v.tier == "none" and v.model_calls > 0
            and v.headline.startswith("Could not retrieve evidence")):
        return "unavailable"
    return "ok"


def _confidence(v) -> Dict[str, Any]:
    if v.verdict == V.NO_VERDICT:
        return {"tier": None, "text": ""}
    if v.tier in ("structured", "dataset"):
        text = {
            V.HIGH: "The agency's own published figure for the period and place in the claim.",
            V.MEDIUM: "The agency's own published figure, with a note to read.",
        }.get(v.confidence, "Official figure, but the match to the claim is uncertain.")
    else:
        text = {
            V.MEDIUM: "Official sources found by web search back this verdict.",
        }.get(v.confidence, "Only non-official sources were found; treat this with care.")
    return {"tier": v.confidence, "text": text}


def _method(v, sources: List[Dict[str, Any]]) -> Dict[str, Any]:
    secs = round(v.elapsed_seconds or 0.0, 1)
    if v.tier == "structured":
        agencies = sorted({s["agency_short"] for s in sources if s["kind"] == "data"})
        who = " and ".join(agencies) or "the agency"
        return {
            "kind": "official_data",
            "title": "Straight from the source",
            "text": f"The figure was read directly from {who}'s published data. No AI was used.",
            "model_calls": v.model_calls, "elapsed_seconds": secs,
        }
    if v.tier == "dataset":
        s0 = next((s for s in sources if s["kind"] == "data"), None)
        where = f"{s0['agency']}'s {s0['dataset']}" if s0 and s0.get("dataset") else "the agency's data"
        return {
            "kind": "dataset",
            "title": "Found in a government dataset",
            "text": (f"A short AI step chose the matching series in {where}; the figure itself was "
                     f"read directly from the data, not written by AI."),
            "model_calls": v.model_calls, "elapsed_seconds": secs,
        }
    if v.tier == "grounded":
        web = [s for s in sources if s["kind"] == "web"]
        official = sum(1 for s in web if s["official"])
        n = len(web)
        return {
            "kind": "web_search",
            "title": "Found by web search",
            "text": (f"No direct data feed covers this, so an AI model searched the web and read "
                     f"{n} source{'s' if n != 1 else ''} ({official} official). It may only give "
                     f"a verdict that a source backs."),
            "model_calls": v.model_calls, "elapsed_seconds": secs,
        }
    return {
        "kind": "none",
        "title": "Not checked",
        "text": "No official figure or published source was consulted for this claim.",
        "model_calls": v.model_calls, "elapsed_seconds": secs,
    }


def _web_citation(web: List[Dict[str, Any]], reported: Dict[str, str]):
    """A citation for a web-search answer: the best official page it rests on.
    Returns (plain, html)."""
    if not web:
        return "", ""
    # Only what the page itself says (its title, site and address): the
    # model's own wording of a publisher or period is not citable.
    s = next((x for x in web if x["official"]), web[0])
    body = f"“{s['title']}.” {s['domain']}."
    tail = f" (accessed {_long_date(date.today().isoformat())})."
    plain = f"{body} {s['url']}{tail}"
    html = (f'{escape(body)} <a href="{escape(s["url"], quote=True)}">{escape(s["url"])}</a>'
            f"{escape(tail)}")
    return plain, html


def _share_text(label: str, v, comparisons, citation: str) -> str:
    lines = [f"{label}: “{v.claim.strip()}”"]
    for c in comparisons:
        if c["official"]:
            prefix = f"{c['label']}: " if c["label"] and len(comparisons) > 1 else ""
            lines.append(f"{prefix}Official figure: {c['official']}"
                         + (f" ({c['context']})" if c["context"] else "")
                         + (f". {c['note']}" if c["note"] else "."))
    if not comparisons and v.headline:
        lines.append(v.headline.strip())
    if citation:
        lines.append(f"Source: {citation}")
    lines.append("Checked with Project Mirador")
    return "\n".join(lines)


def build_display(v) -> Dict[str, Any]:
    status = _status(v)
    key, meaning = _VERDICTS.get(v.verdict, ("no-verdict", ""))
    label = _LABELS.get(v.verdict, v.verdict)
    if status == "unavailable":
        key, label = "unavailable", "Couldn't check"
        meaning = "An outage on our side, not a judgment about the claim."

    sources = [_data_source(e) for e in (v.evidence or [])]
    sources += [s for s in (_web_source(c) for c in (v.citations or [])) if s]
    web = [s for s in sources if s["kind"] == "web"]
    comparisons = [_comparison(p) for p in (v.points or [])]

    summary = (v.headline or "").strip()
    reported = getattr(v, "reported", None) or {}
    reported_value, reported_context = "", ""
    if v.tier == "grounded" and reported.get("value"):
        reported_value = reported["value"]
        reported_context = " · ".join(x for x in (reported.get("period"),
                                                      reported.get("source_org")) if x)
        # The figure is shown on its own; the summary is the explanation.
        summary = reported.get("explanation") or summary

    reason = None
    if v.verdict == V.NO_VERDICT and v.reason:
        why, try_next = _REASONS.get(v.reason, ("", ""))
        if status == "limited":
            why, try_next = "", "Exact-figure checks still work."
        reason = {"code": v.reason, "why": why, "next": try_next}

    notes: List[str] = []
    for c in v.caveats or []:
        c = (c or "").strip()
        if c and c not in notes:
            notes.append(c)

    if v.tier == "grounded":
        citation, citation_html = _web_citation(web, reported)
    else:
        citation_html = "<br>".join(c["citation_html"] for c in comparisons if c["citation_html"])
        citation = "\n".join(c["citation"] for c in comparisons if c["citation"])

    return {
        "status": status,
        "verdict": {"key": key, "label": label, "meaning": meaning},
        "summary": summary,
        "confidence": _confidence(v),
        "reason": reason,
        "comparisons": comparisons,
        "reported_value": reported_value,
        "reported_context": reported_context,
        "sources": sources,
        "method": _method(v, sources),
        "notes": notes,
        "citation": citation,
        "citation_html": citation_html,
        "share_text": _share_text(label, v, comparisons, citation.split("\n")[0] if citation else ""),
    }
