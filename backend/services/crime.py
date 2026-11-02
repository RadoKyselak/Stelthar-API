"""U.S. crime claims checked against the FBI's Uniform Crime Reporting data
(Crime Data Explorer API), not web pages.

  "The U.S. violent crime rate was 380 per 100,000 in 2022"
  "There were about 19,000 murders in 2023"
  "Texas had a violent crime rate of 432 per 100,000 in 2022"
  "More than 1 million violent crimes were reported in 2022"

Measures: violent and property crime, murder (and nonnegligent
manslaughter), robbery, aggravated assault, burglary and motor vehicle theft,
as a rate per 100,000 people or a count, for the nation or a state, for one
calendar year.

What the API serves is the offenses agencies reported, with the share of the
population those agencies cover; the FBI's national estimates (its "Crime in
the Nation" figures, which most claims quote) adjust for agencies that did
not report, and are not in the API. The two differ by a few percent: the
reported rate runs a little high (reporting areas are more urban) and late
reports raise counts after an estimate is published. So a claim is judged
against a band around the reported figure:

  inside the band          Supported (Medium confidence, the reason said)
  just outside it          None: too close to call, the claim goes to search
  well outside it          Contradicted

Counts need at least 90% coverage (2021, the first NIBRS-only year, covers
about three quarters of the population); rates, being per covered person,
need 70%. City and county claims, subgroups, trends and arrests are left
alone (None).
"""
import re
from dataclasses import dataclass, replace
from typing import List, Optional, Tuple

from claims.verdict import CONTRADICTED, MEDIUM, SUPPORTED, Verdict
from config import logger
from evidence.datapoint import Geography
from evidence.excerpt import row as ex_row
from evidence.sources import SourceUnavailable, fbi_crime
from evidence.units import parse_quantity
from services.regulatory import fact, verdict

# key: (FBI offense code, pattern, rate label, count noun)
_MEASURES = [
    ("V", r"\bviolent[- ]crimes?\b", "violent crime rate", "violent crimes"),
    ("P", r"\bproperty[- ]crimes?\b", "property crime rate", "property crimes"),
    ("HOM", r"\bmurders?\b|\bmurdered\b|\bhomicides?\b", "murder rate",
     "murders and nonnegligent manslaughters"),
    ("ROB", r"\brobber(?:y|ies)\b", "robbery rate", "robberies"),
    ("ASS", r"\baggravated assaults?\b", "aggravated assault rate", "aggravated assaults"),
    ("BUR", r"\bburglar(?:y|ies)\b", "burglary rate", "burglaries"),
    ("MVT", r"\b(?:motor[- ]vehicle|car|auto)[- ]thefts?\b", "motor vehicle theft rate", "motor vehicle thefts"),
]
_FBI_CUE = re.compile(r"\bFBI\b|\bUCR\b|\bUniform Crime\b|\breported (?:to|by) (?:the )?(?:police|FBI|law enforcement)\b|"
                      r"\blaw enforcement\b|\bcrime data\b", re.I)
# Not the measure the FBI series is: who did it, to whom, where, how, or a share.
_OTHER = re.compile(
    r"\b(arrest\w*|clear(?:ed|ance)\w*|solved|unsolved|victimi[sz]ations?|victims?|NCVS|hate crimes?|guns?|"
    r"firearms?|shootings?|shot|stabbings?|knives?|mass|police (?:killed|shot)|by police|officers?|schools?|"
    r"campus|juveniles?|teen\w*|youths?|women|men|children|kids|Black|white|Hispanic|Latino|Asian|"
    r"immigrants?|illegal|migrants?|undocumented|per day|a day|an hour|every \d+|daily|monthly|weekly|"
    r"percent|share|times (?:higher|more|as)|attempted|first[- ]degree|serial|domestic|gang|drug[- ]related|"
    r"cities|city|urban|rural|suburb\w*|counties|county|metro\w*|downtown|neighbou?rhoods?|"
    r"in (?:all|most|many|some) states|per state|world|global\w*|countries|Europe\w*|Canada|"
    r"first half|quarter|Q[1-4]|so far|year[- ]to[- ]date|through (?:June|September)|"
    r"months?)\b|%", re.I)
# Change and comparison claims need two points; this check reads one.
_TREND = re.compile(
    r"\b(rose|rise[sn]?|rising|fell|falls?|falling|fallen|increase[ds]?|increasing|decrease[ds]?|decreasing|"
    r"drop(?:ped|s|ping)?|declin\w+|jump\w*|surg\w+|spik\w+|soar\w*|plung\w+|plummet\w*|doubl\w+|tripl\w+|"
    r"halv\w+|highest|lowest|record|since|compared|change[ds]?|grew|growth|went (?:up|down)|"
    r"up \d|down \d)\b|\bfrom\s+(?:about\s+|roughly\s+)?\d", re.I)
_RATE = re.compile(r"\brates?\b|\bper\s+100,?000\b|\bper\s+100k\b|\bper capita\b", re.I)
_YEAR = re.compile(r"\b(19[89]\d|20[0-4]\d)\b")
# A figure, as written: "19,000", "1.2 million", "380.7", "21K".
_NUM = re.compile(r"(?<![\w.,$])(\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:\s*(?:million|thousand|billion|k)\b)?", re.I)
_PER = re.compile(r"\bper\s+(?:every\s+)?(?:100,?000|100k|100 thousand)\b(?:\s+(?:people|residents|inhabitants|population))?", re.I)

# Capitalised words a national claim may carry; any other names a place
# ("Chicago had 617 murders") that the claim parser may not have resolved.
_ALLOWED_NAMES = {
    "u.s.", "us", "usa", "u.s", "united", "states", "america", "american", "americans", "america's",
    "fbi", "fbi's", "ucr", "nibrs", "cdc", "cdc's", "nchs", "national", "center", "centers", "health",
    "statistics", "disease", "control", "prevention", "uniform", "crime", "reporting", "program", "data",
    "explorer", "federal", "bureau", "investigation", "vital", "i", "alzheimer's", "alzheimer", "alzheimers",
    "nationwide", "rapid", "release", "vsrr",
    "january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
    "november", "december",
}
_FIRST_WORDS = re.compile(
    r"^(there|the|in|about|around|roughly|nearly|almost|over|more|fewer|less|some|approximately|according|"
    r"last|during|only|just|an?|each|every|overall|total|all|average|official|federal|reported|police|law|"
    r"agencies|data|estimates?|nationwide|nationally|deaths?|drug|drugs|life|fentanyl|opioids?|synthetic|"
    r"heroin|cocaine|meth\w*|psychostimulants?|heart|cancer|strokes?|diabetes|suicides?|unintentional|"
    r"accidents?|accidental|kidney|chronic|influenza|flu|violent|property|murders?|homicides?|"
    r"robber(?:y|ies)|aggravated|burglar(?:y|ies)|motor|car|auto|vehicle|overdoses?|crimes?|"
    r"male|female|men|women)$", re.I)


def unnamed_place(text: str) -> bool:
    """True when the claim names no place beyond the United States."""
    for i, m in enumerate(re.finditer(r"(?<![\w'])[A-Z][A-Za-z'’.]*", text)):
        w = m.group(0).rstrip(".").replace("’", "'")
        lw = w.lower()
        if lw in _ALLOWED_NAMES or (lw + ".") in _ALLOWED_NAMES:
            continue
        sentence_start = m.start() == 0 or re.search(r"[.!?:;]\s*$", text[:m.start()])
        if sentence_start and _FIRST_WORDS.match(w):
            continue
        return False
    return True


def figures(text: str) -> List[re.Match]:
    """The figures a claim states: not its years, not the "100,000" of a rate."""
    blank = _PER.sub(lambda m: " " * len(m.group(0)), text)
    out = []
    for m in _NUM.finditer(blank):
        s = m.group(0)
        if _YEAR.fullmatch(s.strip()):
            continue
        out.append(m)
    return out


def place_of(parsed, text: str) -> Optional[Tuple[Optional[str], str, Optional[Geography]]]:
    """(FBI state abbreviation or None, place name, Geography) or None when the
    claim is about a place this check cannot read."""
    geo = getattr(parsed, "geography", None)
    if geo is None:
        if not unnamed_place(text):
            return None
        return None, "United States", None
    if geo.level == "nation":
        return None, "United States", None
    if geo.level == "state" and geo.fips in fbi_crime.STATE_ABBR:
        return fbi_crime.STATE_ABBR[geo.fips], geo.name, geo
    return None


def one_year(text: str, parsed) -> Optional[int]:
    years = {int(y) for y in _YEAR.findall(text)}
    if len(years) != 1:
        return None
    year = years.pop()
    if parsed is not None and getattr(parsed, "year", None) not in (None, year):
        return None
    return year


def comparator(text: str, figure: re.Match) -> str:
    before = text[max(0, figure.start() - 24):figure.start()].lower()
    if re.search(r"\b(more than|over|above|exceed(?:ed|s|ing)?|topped|at least|upwards of)\s*$", before):
        return "greater than"
    if re.search(r"\b(less than|under|below|fewer than|nearly|almost|just shy of)\s*$", before):
        return "less than"
    return "equals"


def hedge(text: str, figure: re.Match) -> bool:
    """"about 19,000": the claim rounds."""
    before = text[max(0, figure.start() - 20):figure.start()].lower()
    return bool(re.search(r"\b(about|around|roughly|approximately|some|nearly|almost|close to|an estimated)\s*$|~\s*$",
                          before))


@dataclass
class Read:
    offense: str
    kind: str              # "rate" | "count"
    label: str             # "violent crime rate" | "violent crimes"
    figure: str            # as written
    claimed: float
    tolerance: float       # absolute, from how precisely the claim was written
    comparator: str
    year: int
    state: Optional[str]   # FBI abbreviation, None for the nation
    place: str
    geography: Optional[Geography]
    text: str = ""


def read(text: str, parsed=None) -> Optional[Read]:
    hits = [(code, rx, rl, cn) for code, rx, rl, cn in _MEASURES if re.search(rx, text, re.I)]
    if len(hits) != 1:
        return None
    code, rx, rate_label, count_noun = hits[0]
    if code == "HOM" and not re.search(r"\bmurder", text, re.I) and not _FBI_CUE.search(text):
        return None  # "homicides" alone may be CDC's death-certificate count, which is higher
    if _OTHER.search(text) or _TREND.search(text):
        return None
    year = one_year(text, parsed)
    if year is None or year < 1985:
        return None
    where = place_of(parsed, text)
    if where is None:
        return None
    state, place, geo = where
    figs = figures(text)
    if len(figs) != 1:
        return None
    fig = figs[0]
    q = parse_quantity(fig.group(0))
    if q is None or q.magnitude <= 0:
        return None
    kind = "rate" if _RATE.search(text) else "count"
    hedged = q.approximate or hedge(text, fig)
    tol = (q.tolerance() or 0.0) * (10.0 if hedged and not q.approximate else 1.0)
    tol = min(tol, q.magnitude * (0.05 if hedged else 0.02))
    if kind == "rate" and q.magnitude > 10000:
        return None
    return Read(offense=code, kind=kind, label=rate_label if kind == "rate" else count_noun,
                figure=fig.group(0).strip(), claimed=q.magnitude, tolerance=tol,
                comparator=comparator(text, fig), year=year, state=state, place=place,
                geography=geo, text=text)


def band(value: float, coverage: float) -> Tuple[float, float]:
    """Where the FBI's published estimate lies, given the reported figure:
    the reported rate runs up to ~8% above the estimate, and agencies that
    did not report would add up to their share of the population."""
    gap = max(0.0, (100.0 - coverage) / 100.0)
    low = value * (1 - 0.08 - max(0.0, gap - 0.10))
    high = value * (1 + max(0.03, gap))
    return low, high


def judge(r: Read, low: float, high: float) -> Optional[str]:
    """SUPPORTED, CONTRADICTED, or None when the claim is too close to call."""
    lo, hi = r.claimed - r.tolerance, r.claimed + r.tolerance
    if r.comparator == "greater than":
        return SUPPORTED if r.claimed <= low else CONTRADICTED if r.claimed >= high * 1.1 else None
    if r.comparator == "less than":
        return SUPPORTED if r.claimed >= high else CONTRADICTED if r.claimed <= low * 0.9 else None
    if hi >= low and lo <= high:
        return SUPPORTED
    if hi >= low * 0.9 and lo <= high * 1.1:
        return None
    return CONTRADICTED


async def check(text: str, parsed) -> Optional[Verdict]:
    r = read(text, parsed)
    if r is None:
        return None
    try:
        d = await fbi_crime.annual(r.offense, r.year, r.state, r.place)
    except SourceUnavailable as e:
        logger.warning("Crime check skipped for %r: %s", text[:80], e)
        return None
    except Exception:  # noqa: BLE001
        logger.exception("Crime check failed for %r", text[:120])
        return None
    if not d or d.get("coverage") is None:
        return None  # the year is not fully published
    coverage = float(d["coverage"])
    if coverage < (90.0 if r.kind == "count" else 70.0):
        logger.info("Crime: %s %s covers %.0f%% of the population; too incomplete for a %s",
                    r.place, r.year, coverage, r.kind)
        return None
    value = float(d["rate"] if r.kind == "rate" else d["count"])
    if value <= 0:
        return None
    low, high = band(value, coverage)
    outcome = judge(r, low, high)
    if outcome is None:
        logger.info("Crime: %r is near the reported %s (%s) but outside the band", text[:80], r.label, value)
        return None

    nation = r.state is None
    shown = f"{value:,.{1 if value >= 100 else 2}f} per 100,000 people" if r.kind == "rate" else f"{int(value):,}"
    whose = "the U.S. population" if nation else f"the population of {r.place}"
    source = f"FBI Uniform Crime Reporting (offenses reported by agencies covering {coverage:.0f}% of {whose})"
    if r.kind == "rate":
        where = "" if nation else f" in {r.place}"
        head = f"{source}: the {r.label}{where} was {shown} in {r.year}"
    else:
        where = "" if nation else f" in {r.place}"
        head = f"{source}: {shown} {r.label} were reported{where} in {r.year}"
    head += f"; the claim says {r.figure}." if outcome != SUPPORTED else "."
    label = (f"{r.label.capitalize()}{'' if nation else ', ' + r.place}" if r.kind == "rate"
             else f"{r.label.capitalize()} reported{'' if nation else ', ' + r.place}")
    rows = [ex_row(f"{label}, {r.year}", shown, cited=True),
            ex_row("Population covered by reporting agencies", f"{coverage:g}%")]
    if d.get("population"):
        rows.append(ex_row("Population", f"{int(d['population']):,}"))
    note = ("The annual rate is the sum of the FBI's 12 monthly rates (offenses per 100,000 people in the "
            "areas that reported)." if r.kind == "rate" else "The sum of the 12 monthly counts agencies reported.")
    if d.get("refreshed"):
        note += f" Data as of {d['refreshed']}."
    dp = fact("FBI", label, shown, fbi_crime.PAGE, year=r.year, magnitude=value,
              basis="Uniform Crime Reporting Program, offenses reported by law enforcement agencies",
              series_id=f"{r.state or 'US'}/{r.offense}", rows=rows,
              units="per 100,000 people" if r.kind == "rate" else "offenses", note=note)
    if r.geography is not None:
        dp = replace(dp, geography=r.geography)
    caveats = [
        "These are offenses reported to the FBI; its published national and state estimates adjust for "
        "agencies that did not report and usually differ by a few percent, so a figure within that range "
        "counts as a match."]
    if r.year == 2021:
        caveats.append(
            "2021 was the first year the FBI accepted only NIBRS reports, and many agencies, including some "
            "of the largest, did not submit a full year; 2021 figures are much less complete than other years.")
    return verdict(outcome, head, [dp], caveats=caveats, claimed=r.figure, confidence=MEDIUM)


SEARCH_HINT = (
    "The claim is about U.S. crime statistics. Check the FBI's Crime Data Explorer "
    "(cde.ucr.cjis.gov) and its annual Crime in the Nation release for national and state "
    "estimates (rates per 100,000 people and counts, by calendar year). Murder figures are "
    "\"murder and nonnegligent manslaughter\"; CDC's death-certificate homicide count is "
    "higher. Name the year and say whether the figure is an estimate or reported data.")
