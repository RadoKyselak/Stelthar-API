"""Render a Datapoint as a citation a writer can paste.

The verification path asks "is this claim true". A writer has the opposite
problem: they know what they want to say and need the number, correctly
attributed, without leaving the sentence they are writing. Today that means
opening FRED or data.bls.gov, finding the series, choosing a vintage, copying
the figure and hand-building a footnote — several minutes, many times a day.

Every field a citation needs is already on the Datapoint, because the engine
reads provenance from the payload rather than reconstructing it from the
request. This module only formats what is already there.

One rule drives the output: **the basis is part of the number.** When BLS
publishes no annual average and the adapter computes one from twelve monthly
observations, the citation says so. When a Treasury figure is a point-in-time
record rather than a fiscal-year close, the citation says so. Dropping that
detail is exactly the error the pipeline was built to prevent — a December
print passed off as an annual average — and a citation that hides it
reintroduces the bug at the point where it does the most damage, inside
someone else's published work.

Formats, in increasing completeness:

    value      3.6%
    inline     3.6% (BLS, 2023 annual average)
    markdown   3.6% ([BLS, 2023 annual average](https://…))
    html       3.6% (<a href="https://…">BLS, 2023 annual average</a>)
    note       U.S. Bureau of Labor Statistics, "Unemployment rate," series
               LNS14000000, United States, 2023 annual average: 3.6%.
               https://… (accessed 27 August 2026).

`html` exists so the clipboard can carry `text/html` alongside `text/plain`:
pasting into Google Docs or Word then yields a live hyperlink, while pasting
into a plain-text editor yields the inline form. That dual write is the whole
ergonomic point — a citation that arrives unlinked has to be rebuilt by hand.
"""
from datetime import date
from html import escape
from typing import Dict, Optional

from evidence.datapoint import Datapoint

# Agencies write their own names a particular way; a citation that gets the
# publisher wrong is not a citation.
_AGENCY_NAMES = {
    "BLS": "U.S. Bureau of Labor Statistics",
    "BEA": "U.S. Bureau of Economic Analysis",
    "CENSUS": "U.S. Census Bureau",
    "TREASURY": "U.S. Department of the Treasury, Fiscal Data",
    "USASPENDING": "USAspending.gov",
    "CONGRESS": "U.S. Congress, Congress.gov",
}

# How an agency is referred to mid-sentence, where the full legal name would
# swamp the number it is attached to.
_AGENCY_SHORT = {
    "BLS": "BLS",
    "BEA": "BEA",
    "CENSUS": "Census",
    "TREASURY": "Treasury",
    "USASPENDING": "USAspending",
    "CONGRESS": "Congress.gov",
}

_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def agency_name(source: str) -> str:
    """The agency's own name for itself, or the raw code if unrecognised."""
    return _AGENCY_NAMES.get((source or "").upper(), source or "Unknown source")


def agency_short(source: str) -> str:
    """The mid-sentence form, e.g. "Treasury" rather than "TREASURY"."""
    key = (source or "").upper()
    return _AGENCY_SHORT.get(key, source.title() if source else "Unknown")


def _format_date(d: date) -> str:
    return f"{d.day} {_MONTHS[d.month - 1]} {d.year}"


def _short_attribution(dp: Datapoint) -> str:
    """"BLS, 2023 annual average" — what goes in parentheses after a number.

    Geography is included only when it is not the nation, because "United
    States" in an article about the United States is noise, while "Texas" is
    the entire point.
    """
    parts = [agency_short(dp.source)]
    if dp.geography and dp.geography.level != "nation":
        parts.append(dp.geography.name)
    parts.append(dp.observed.describe(short=True))
    return ", ".join(p for p in parts if p)


def render(dp: Datapoint, accessed: Optional[date] = None) -> Dict[str, str]:
    """Every citation form for one datapoint, ready to put on a clipboard."""
    accessed = accessed or date.today()
    value = dp.display_value()
    attribution = _short_attribution(dp)
    url = dp.source_url or ""

    inline = f"{value} ({attribution})"
    markdown = f"{value} ([{attribution}]({url}))" if url else inline

    if url:
        html = (
            f"{escape(value)} (<a href=\"{escape(url, quote=True)}\">"
            f"{escape(attribution)}</a>)"
        )
    else:
        html = escape(inline)

    note_parts = [f'{agency_name(dp.source)}, "{dp.label},"']
    if dp.series_id:
        note_parts.append(f"series {dp.series_id},")
    if dp.geography:
        note_parts.append(f"{dp.geography.name},")
    note_parts.append(f"{dp.observed.describe()}: {value}.")
    if url:
        note_parts.append(url)
    note_parts.append(f"(accessed {_format_date(accessed)}).")
    note = " ".join(note_parts)

    return {
        "value": value,
        "inline": inline,
        "markdown": markdown,
        "html": html,
        "note": note,
        "attribution": attribution,
        "caveat": dp.observed.long_basis,
        "agency": agency_name(dp.source),
        "url": url,
        "accessed": accessed.isoformat(),
    }
