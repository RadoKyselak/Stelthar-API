"""The exact rows a figure was read from, as the agency published them.

A link sends a reader to a page with hundreds of numbers. An excerpt shows the
cell itself, beside its neighbours in the agency's own layout, so the reader
can find it on the page and see how a derived figure was worked out.

Values are the agency's own strings. The only change is thousands separators
on long numbers, which adds no digit and drops none.
"""
import re
from typing import Any, Dict, List, Optional

from evidence.units import INDEX, JOBS, MINUTES, PERCENT, PERSONS, USD, YEARS

_NUMBER = re.compile(r"(-?)(\d+)(\.\d+)?")


def published(raw: Any) -> str:
    """"34001493655565.48" -> "34,001,493,655,565.48"; "4.0" stays "4.0"."""
    s = str(raw if raw is not None else "").strip()
    m = _NUMBER.fullmatch(s)
    if not m:
        return s
    sign, whole, frac = m.group(1), m.group(2), m.group(3) or ""
    return f"{sign}{int(whole):,}{frac}"


def row(label: str, value: Any, cited: bool = False) -> Dict[str, Any]:
    out: Dict[str, Any] = {"label": str(label), "value": published(value)}
    if cited:
        out["cited"] = True
    return out


def make(rows: List[Dict[str, Any]], units: str = "", note: str = "") -> Optional[Dict[str, Any]]:
    """Exactly one row is the cited one; an excerpt without it is not built."""
    rows = [r for r in rows if r and r.get("value")]
    if sum(1 for r in rows if r.get("cited")) != 1:
        return None
    out: Dict[str, Any] = {"rows": rows}
    if units:
        out["units"] = units
    if note:
        out["note"] = note
    return out


def units_for(family: str) -> str:
    return {PERCENT: "percent", USD: "dollars", JOBS: "jobs", PERSONS: "people",
            INDEX: "index", YEARS: "years", MINUTES: "minutes"}.get(family, "")


def moe_excerpt(estimate: Any, moe: Any, units: str = "") -> Optional[Dict[str, Any]]:
    """A Census survey cell: the estimate and its 90% margin of error, labelled
    as Census tables label them."""
    rows = [row("Estimate", estimate, cited=True)]
    if moe not in (None, ""):
        rows.append({"label": "Margin of Error", "value": "±" + published(str(moe).lstrip("-"))})
    return make(rows, units)
