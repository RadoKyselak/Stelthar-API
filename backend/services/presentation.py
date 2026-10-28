"""Plain-language presentation of a verification result.

Every client (the extension's in-page panel, its popup, the web page) renders
this one `display` block. The wording therefore lives in one place, and it
improves for every installed copy as soon as the API deploys, with no Chrome
Web Store review in between.

Nothing here changes a verdict. It only explains one: what the verdict means,
which figure it rests on and where that figure came from, how the check was
done, and what to try when there is no answer.
"""
import re
from datetime import date
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from claims import verdict as V
from evidence.citation import agency_name, agency_short

# key, meaning
_VERDICTS = {
    V.SUPPORTED: ("supported", "Matches the official figure."),
    V.MOSTLY_ACCURATE: ("mostly-accurate", "Close to the official figure, but not exact."),
    V.CONTRADICTED: ("contradicted", "The official figure says otherwise."),
    V.MISLEADING: ("misleading", "The number may be right, but the framing misleads."),
    V.NO_VERDICT: ("no-verdict", "No official figure settles this claim."),
}

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
        "The official figure is shown for reference.",
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

# Dataset names, so a reader sees "Local Area Unemployment Statistics", not
# only a series code.
_BLS_PROGRAMS = (
    (("LNS", "LNU"), "Current Population Survey"),
    (("LAU", "LAS"), "Local Area Unemployment Statistics"),
    (("CUU", "CUS"), "Consumer Price Index"),
    (("APU",), "Average Price Data"),
    (("CES", "CEU"), "Current Employment Statistics"),
)

# Pages a person can read, for sources whose data URL is a raw API response.
_READABLE_PAGES = {
    "debt_to_penny": "https://fiscaldata.treasury.gov/datasets/debt-to-the-penny/debt-to-the-penny",
}

_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")
_MONTH_CODE_RE = re.compile(r"\bM(0[1-9]|1[0-2]) (\d{4}) vs M(0[1-9]|1[0-2]) (\d{4})")
_AS_OF_RE = re.compile(r",?\s*as of \d{4}-\d{2}-\d{2}")


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


def _clean_basis(basis: str) -> str:
    basis = _MONTH_CODE_RE.sub(
        lambda m: f"12-month change from {_MONTHS[int(m.group(3)) - 1]} {m.group(4)}", basis or "")
    return _AS_OF_RE.sub("", basis).strip(" ,")


def _period_text(e: Dict[str, Any]) -> str:
    if e.get("period_kind") == "point_in_time" and e.get("as_of"):
        return _long_date(e["as_of"])
    return e.get("period_label") or e.get("period") or ""


def _dataset(source: str, series_id: str, basis: str) -> str:
    source = (source or "").upper()
    sid = (series_id or "").upper()
    if source == "BLS":
        for prefixes, name in _BLS_PROGRAMS:
            if sid.startswith(prefixes):
                return name
        return ""
    if source == "BEA":
        return "National Income and Product Accounts"
    if source == "CENSUS":
        return "American Community Survey, 1-year estimates" if "1-year" in basis else \
            "American Community Survey"
    if source == "TREASURY":
        if sid == "DEBT_TO_PENNY":
            return "Debt to the Penny"
        if sid.startswith("MTS"):
            return "Monthly Treasury Statement"
        return "Fiscal Data"
    return ""


def _series_label(source: str, series_id: str) -> str:
    """The identifier a reader can search for at the agency, if it has one."""
    if not series_id:
        return ""
    if (source or "").upper() == "TREASURY":
        return "Table 1" if series_id.upper().startswith("MTS") else ""
    return series_id


def _data_source(e: Dict[str, Any]) -> Dict[str, Any]:
    src = e.get("source") or ""
    sid = e.get("series_id") or ""
    basis = _clean_basis(e.get("period_basis") or "")
    data_url = _safe_url(e.get("source_url"))
    url = _READABLE_PAGES.get(sid, data_url)
    return {
        "kind": "data",
        "official": True,
        "agency": agency_name(src),
        "agency_short": agency_short(src),
        "dataset": _dataset(src, sid, basis),
        "measure": e.get("label") or "",
        "value": e.get("value_display") or "",
        "period": _period_text(e),
        "basis": basis,
        "place": e.get("geography") or "",
        "series_id": _series_label(src, sid),
        "series_prefix": "" if src.upper() == "TREASURY" else "Series",
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


def _comparison(p: Dict[str, Any]) -> Dict[str, Any]:
    verdict, gap, comparator = p.get("verdict"), p.get("gap") or "", p.get("comparator")
    if not p.get("claim"):
        note = "For reference"
    elif comparator != "equals":
        note = ("The official figure meets the claim's threshold." if verdict == V.SUPPORTED
                else "The official figure does not meet the claim's threshold.")
    elif verdict == V.SUPPORTED:
        note = f"Matches at the precision the claim used (off by {gap})." if gap else "Exact match."
    elif gap:
        note = f"Off by {gap}."
    else:
        note = ""
    place = p.get("place") or ""
    context = " · ".join(x for x in (
        "" if place == "United States" else place, p.get("period"), _clean_basis(p.get("basis") or ""),
    ) if x)
    return {
        "label": p.get("label"),
        "verdict_key": _VERDICTS.get(verdict, ("no-verdict", ""))[0],
        "claim": p.get("claim") or "",
        "official": p.get("official") or "",
        "context": context,
        "source": agency_short(p.get("source") or ""),
        "note": note,
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
    if v.tier == "structured":
        text = {
            V.HIGH: "The agency's own published figure for the period and place in the claim.",
            V.MEDIUM: "The agency's own published figure, with a note to read below.",
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
            "title": "Checked against official data",
            "text": f"The figure was read directly from {who}'s published data. No AI was used.",
            "model_calls": v.model_calls, "elapsed_seconds": secs,
        }
    if v.tier == "grounded":
        web = [s for s in sources if s["kind"] == "web"]
        official = sum(1 for s in web if s["official"])
        n = len(web)
        return {
            "kind": "web_search",
            "title": "Checked by web search",
            "text": (f"An AI model searched the web and read {n} source{'s' if n != 1 else ''}"
                     f" ({official} official). It may only give a verdict a source backs."),
            "model_calls": v.model_calls, "elapsed_seconds": secs,
        }
    return {
        "kind": "none",
        "title": "Not checked",
        "text": "No official figure or published source was consulted for this claim.",
        "model_calls": v.model_calls, "elapsed_seconds": secs,
    }


def _share_text(label: str, v, sources: List[Dict[str, Any]]) -> str:
    lines = [f"{label}: “{v.claim.strip()}”", v.headline.strip()]
    data = [s for s in sources if s["kind"] == "data"]
    web = [s for s in sources if s["kind"] == "web"]
    for s in data[:2]:
        sid = f", series {s['series_id']}" if s["series_id"] else ""
        lines.append(f"Source: {s['agency']}, {s['measure']}{sid}. {s['url']}".strip())
    for s in web[:2]:
        lines.append(f"Source: {s['title']} ({s['domain']}). {s['url']}")
    lines.append("Checked with Project Mirador")
    return "\n".join(x for x in lines if x)


def build_display(v) -> Dict[str, Any]:
    status = _status(v)
    key, meaning = _VERDICTS.get(v.verdict, ("no-verdict", ""))
    label = v.verdict
    if status == "unavailable":
        key, label = "unavailable", "Couldn't check"
        meaning = "An outage on our side, not a judgment about the claim."

    sources = [_data_source(e) for e in (v.evidence or [])]
    sources += [s for s in (_web_source(c) for c in (v.citations or [])) if s]

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

    return {
        "status": status,
        "verdict": {"key": key, "label": label, "meaning": meaning},
        "summary": (v.headline or "").strip(),
        "confidence": _confidence(v),
        "reason": reason,
        "comparisons": [_comparison(p) for p in (v.points or [])],
        "reported_value": v.official_value if (v.tier == "grounded" and v.official_value) else "",
        "sources": sources,
        "method": _method(v, sources),
        "notes": notes,
        "share_text": _share_text(label, v, sources),
    }
