"""Claims about what a regulator did: FDA approvals, labels, recalls and
shortages (services/fda.py) and FTC fraud data and rules (services/ftc.py).

These are facts, not figures: "Ozempic is approved for weight loss" is
settled by the label FDA approved, "the FTC banned noncompetes" by the
Federal Register. Each check reads the agency's own record, says what it
shows, and links to it. The rules here read the claim for free; a phrasing
they miss reaches them through the interpreter's "regulatory" query.
"""
import re
from dataclasses import replace
from typing import List, Optional, Sequence

from claims.verdict import HIGH, Verdict, assess
from evidence.datapoint import POINT_IN_TIME, Datapoint, Geography, Period
from evidence.excerpt import make as make_excerpt, row as ex_row
from evidence.units import UNKNOWN, Quantity, parse_quantity

NEGATION = re.compile(r"\b(not|never|isn'?t|wasn'?t|hasn'?t|haven'?t|aren'?t|weren'?t|doesn'?t|"
                      r"don'?t|didn'?t|no longer|unapproved|without)\b", re.I)


def fact(source: str, label: str, official: str, url: str, *, year: Optional[int] = None,
         record_date: str = "", basis: str = "", rows: Sequence = (), series_id: str = "",
         units: str = "", note: str = "", magnitude: Optional[float] = None, family: str = UNKNOWN) -> Datapoint:
    """A Datapoint for a fact that is not a number: shown as `official`."""
    shown = list(rows) or [ex_row(label, official, cited=True)]
    return Datapoint(
        quantity=Quantity(magnitude=float(year or 0) if magnitude is None else float(magnitude),
                          family=family, raw=""),
        metric="regulatory_record", label=label,
        observed=Period(year=year, kind=POINT_IN_TIME, basis=basis, record_date=record_date or None),
        geography=Geography.national(), source=source, source_url=url,
        series_id=series_id or None,
        extra={"display": official, "publisher_short": source,
               "excerpt": make_excerpt(shown, units=units, note=note)},
    )


def verdict(outcome: str, headline: str, dps: List[Datapoint], *, caveats=(), claimed: str = "",
            confidence: str = HIGH) -> Verdict:
    return Verdict(verdict=outcome, confidence=confidence, headline=headline,
                   claimed_display=claimed, official_display=dps[0].extra.get("display", "") if dps else "",
                   evidence=list(dps), caveats=list(caveats))


def figure_check(parsed, figure_text: str, dp: Datapoint, text: str, family: str) -> Optional[Verdict]:
    """A number the agency published, judged by the same rules as every figure."""
    claimed = parse_quantity(figure_text)
    if claimed is None:
        return None
    claimed = replace(claimed, family=family)
    if family == UNKNOWN and claimed.magnitude < 1000 and not claimed.approximate:
        claimed = replace(claimed, precision=1.0)  # "60 novel drugs" means 60, not 55-64
    at = text.lower().find(figure_text.lower())
    before = text[max(0, at - 24):max(at, 0)].lower()
    comp = "equals"
    if re.search(r"\b(more than|over|above|exceed(?:ed|s|ing)?|topped|at least)\s*$", before):
        comp = "greater than"
    elif re.search(r"\b(less than|under|below|fewer than|nearly|almost)\s*$", before):
        comp = "less than"
    pc = replace(parsed, metric=None, year=dp.observed.year, month=None, quarter=None, geography=None,
                 claimed_value=claimed, comparator=comp, all_values=[], all_years=[dp.observed.year])
    return assess(pc, [dp])


_FDA_TOPIC = re.compile(r"\bFDA\b|\bFood and Drug Administration\b|\bblack[- ]box\b|\bboxed warning\b|"
                        r"\bapproved (?:for|to treat)\b|\brecalled\b|\bdrug shortages?\b", re.I)
_FTC_TOPIC = re.compile(r"\bFTC\b|\bFederal Trade Commission\b|\bConsumer Sentinel\b|\bnon-?competes?\b|"
                        r"\bclick[- ]to[- ]cancel\b|\bjunk fees?\b", re.I)

_FDA_HINT = (
    "The claim is about an FDA action. Check FDA's own records: Drugs@FDA "
    "(accessdata.fda.gov/scripts/cder/daf) for approvals and dates, the Purple Book for "
    "biologics, the approved label (DailyMed) for approved uses and boxed warnings, FDA "
    "enforcement reports and recall notices, the FDA drug shortage database, and FDA "
    "announcements. Meat, poultry and egg-product recalls are run by USDA's Food Safety and "
    "Inspection Service (fsis.usda.gov), not FDA. For devices, approval (PMA) and clearance "
    "(510(k)) differ: say which applies. In OFFICIAL_VALUE give the record itself, such as "
    "\"approved June 4, 2021\" or \"Class I recall, July 2024\".")
_FTC_HINT = (
    "The claim is about the FTC. Check ftc.gov press releases and the Consumer Sentinel "
    "Network Data Book for fraud figures (reports people filed, by calendar year), and the "
    "Federal Register (federalregister.gov) for a rule's text and status: a rule can be "
    "published, then set aside by a court or withdrawn. In OFFICIAL_VALUE give the figure or "
    "the rule's status as of today.")


_FOOD_COST_TOPIC = re.compile(r"\b(thrifty food plan|food plans?|cost of food|SNAP|food stamps|"
                              r"EBT|WIC|school (?:lunch|meals?))\b", re.I)
_FOOD_COST_HINT = (
    "The claim is about USDA food assistance or food costs. Check USDA's Food and Nutrition "
    "Service (fns.usda.gov): the monthly Cost of Food reports for the four USDA Food Plans "
    "(Thrifty, Low-Cost, Moderate-Cost, Liberal), by family type and month, and SNAP maximum "
    "allotments by household size and fiscal year (the June Thrifty Food Plan cost sets the "
    "next year's maximum SNAP benefit for four). Name the month or fiscal year.")
_NUTRITION_HINT = (
    "The claim is about what is in a food. Check USDA FoodData Central (fdc.nal.usda.gov), "
    "using the food as eaten (raw or cooked as the claim implies) and the portion the claim "
    "names; say which food entry and portion weight you used. Amounts vary with size and "
    "variety, so within about 10% is a match.")


def topic(text: str) -> str:
    """What a claim is about, for routing a miss to search: "fda", "ftc",
    "food_cost", "nutrition", "crime", "health" or ""."""
    t = text or ""
    if _FDA_TOPIC.search(t):
        return "fda"
    if _FTC_TOPIC.search(t):
        return "ftc"
    if _FOOD_COST_TOPIC.search(t):
        return "food_cost"
    from services import crime, health, nutrition
    if nutrition.read(t) is not None:
        return "nutrition"
    if crime.read(t) is not None:
        return "crime"
    if health.read(t) is not None:
        return "health"
    return ""


def search_hint(text: str) -> str:
    """Where the web search should look, when the free check could not settle it."""
    from services import crime, health
    return {"fda": _FDA_HINT, "ftc": _FTC_HINT, "food_cost": _FOOD_COST_HINT,
            "nutrition": _NUTRITION_HINT, "crime": crime.SEARCH_HINT,
            "health": health.SEARCH_HINT}.get(topic(text), "")


async def check(text: str, parsed) -> Optional[Verdict]:
    """A claim checked against the publisher's own record (FDA, FTC, USDA,
    FBI, CDC, World Bank), or None."""
    from services import crime, fda, ftc, health, nutrition, worldbank
    v = await fda.check(text, parsed)
    if v is None:
        v = await ftc.check(text, parsed)
    if v is None:
        v = await nutrition.check(text, parsed)
    if v is None:
        v = await crime.check(text, parsed)
    if v is None:
        v = await health.check(text, parsed)
    if v is None:
        v = await worldbank.check(text, parsed)
    return v
