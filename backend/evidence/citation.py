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
import re
from datetime import date
from html import escape
from typing import Dict, Optional

from evidence.datapoint import POINT_IN_TIME, Datapoint

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
    return f"{_MONTHS[d.month - 1]} {d.day}, {d.year}"


# Programs (datasets) a series belongs to, so a citation names the survey a
# reader can find, not only an opaque code.
_BLS_PROGRAMS = (
    (("LNS", "LNU"), "Current Population Survey"),
    (("LAU", "LAS"), "Local Area Unemployment Statistics"),
    (("CUU", "CUS"), "Consumer Price Index"),
    (("APU",), "Average Price Data"),
    (("CES", "CEU"), "Current Employment Statistics"),
    (("WPU", "WPS"), "Producer Price Index"),
)

# A page a person can read, for sources whose data URL is a raw API response.
_READABLE_PAGES = {
    "debt_to_penny": "https://fiscaldata.treasury.gov/datasets/debt-to-the-penny/debt-to-the-penny",
}

# Basis strings the adapters record, restated for a reader. A basis that only
# repeats the period ("January monthly observation" on a January figure) adds
# nothing and is dropped; one that changes the meaning is always kept.
_BASIS_REWRITES = (
    (re.compile(r"^BLS published annual average$", re.I), "annual average"),
    (re.compile(r"^latest monthly observation$", re.I), "latest month"),
    (re.compile(r"^\w+ monthly observation$", re.I), ""),
    (re.compile(r"^annual$", re.I), ""),
    (re.compile(r",?\s*as of \d{4}-\d{2}-\d{2}"), ""),
)
_MONTH_CODE_RE = re.compile(r"\bM(0[1-9]|1[0-2]) (\d{4}) vs M(0[1-9]|1[0-2]) (\d{4})")

# Short enough to sit inside "(BLS, 2023 annual average)".
_INLINE_BASIS_CHARS = 24


def clean_basis(basis: str) -> str:
    """The basis in plain words, without adapter codes like "M06"."""
    b = _MONTH_CODE_RE.sub(
        lambda m: f"12-month change from {_MONTHS[int(m.group(3)) - 1]} {m.group(4)}", basis or "")
    for rx, repl in _BASIS_REWRITES:
        b = rx.sub(repl, b)
    return b.strip(" ,")


def when(period) -> str:
    """"2023", "June 2022", "FY2024", "Q2 2026", "December 29, 2023"."""
    if period.kind == POINT_IN_TIME and period.record_date:
        try:
            return _format_date(date.fromisoformat(period.record_date[:10]))
        except ValueError:
            return period.record_date
    return period.label()


def dataset_name(source: str, series_id: Optional[str], basis: str = "") -> str:
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
        return "American Community Survey 1-year estimates" if "1-year" in (basis or "") \
            else "American Community Survey"
    if source == "TREASURY":
        if sid == "DEBT_TO_PENNY":
            return "Debt to the Penny"
        if sid.startswith("MTS"):
            return "Monthly Treasury Statement"
        return "Fiscal Data"
    return ""


def identifier(source: str, series_id: Optional[str], *, table: Optional[str] = None,
               as_of: Optional[str] = None) -> str:
    """How the agency itself labels the figure: "series LNU04000000",
    "table B01003", "NIPA Table 1.1.5, series A191RC", "September 2024
    statement, Table 1". Empty when the dataset name says it all."""
    source = (source or "").upper()
    sid = series_id or ""
    if source == "TREASURY":
        if not sid.upper().startswith("MTS"):
            return ""
        # Name the statement: later ones can restate a prior year's total.
        try:
            d = date.fromisoformat((as_of or "")[:10])
            return f"{_MONTHS[d.month - 1]} {d.year} statement, Table 1"
        except ValueError:
            return "Table 1"
    if not sid:
        return ""
    if source == "CENSUS":
        return f"table {sid.split('_')[0]}"
    if source == "BEA":
        from evidence.sources.bea import TABLE_NUMBERS
        number = TABLE_NUMBERS.get(table or "")
        return f"NIPA Table {number}, series {sid}" if number else f"series {sid}"
    return f"series {sid}"


def readable_url(dp: Datapoint) -> str:
    return _READABLE_PAGES.get(dp.series_id or "", dp.source_url or "")


def _short_attribution(dp: Datapoint, basis: str) -> str:
    """"BLS, 2023 annual average" — what goes in parentheses after a number.

    Geography is included only when it is not the nation, because "United
    States" in an article about the United States is noise, while "Texas" is
    the entire point.
    """
    parts = [agency_short(dp.source)]
    if dp.geography and dp.geography.level != "nation":
        parts.append(dp.geography.name)
    period = when(dp.observed)
    if basis and len(basis) <= _INLINE_BASIS_CHARS:
        period = f"{period} {basis}"
    parts.append(period)
    return ", ".join(p for p in parts if p)


def render(dp: Datapoint, accessed: Optional[date] = None) -> Dict[str, str]:
    """Every citation form for one datapoint, ready to put on a clipboard."""
    accessed = accessed or date.today()
    value = dp.display_value()
    basis = clean_basis(dp.observed.basis or "")
    attribution = _short_attribution(dp, basis)
    url = readable_url(dp)

    inline = f"{value} ({attribution})"
    markdown = f"{value} ([{attribution}]({url}))" if url else inline

    if url:
        html = (
            f"{escape(value)} (<a href=\"{escape(url, quote=True)}\">"
            f"{escape(attribution)}</a>)"
        )
    else:
        html = escape(inline)

    # U.S. Bureau of Labor Statistics, "Unemployment rate," Local Area
    # Unemployment Statistics, series LAUST480000000000003, Texas, 2023
    # (annual average): 4.0%. https://... (accessed October 28, 2026).
    head = [f'{agency_name(dp.source)}, "{dp.label},"']
    ident = identifier(dp.source, dp.series_id, table=(dp.extra or {}).get("table"), as_of=dp.as_of)
    for part in (dataset_name(dp.source, dp.series_id, basis), ident,
                 dp.geography.name if dp.geography else ""):
        if part:
            head.append(f"{part},")
    period = when(dp.observed) + (f" ({basis})" if basis else "")
    note = " ".join(head) + f" {period}: {value}."
    if url:
        note += f" {url}"
    note += f" (accessed {_format_date(accessed)})."

    return {
        "value": value,
        "inline": inline,
        "markdown": markdown,
        "html": html,
        "note": note,
        "attribution": attribution,
        "caveat": basis if len(basis) > _INLINE_BASIS_CHARS else "",
        "agency": agency_name(dp.source),
        "url": url,
        "accessed": accessed.isoformat(),
    }
