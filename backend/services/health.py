"""U.S. health claims checked against CDC / NCHS vital statistics on
data.cdc.gov, not web pages.

  life expectancy  "U.S. life expectancy was 78.7 years in 2018"
                   "Life expectancy for American women was 81.4 in 2019"
                   "Life expectancy in Mississippi was 70.9 years in 2021"
                   national 1900-2020, states 2018-2021 (NCHS)
  overdoses        "More than 100,000 Americans died of drug overdoses in 2022"
                   "Fentanyl killed 70,000 Americans in 2021"
                   12 months ending December, national or a state, NCHS
                   provisional counts (VSRR), all drugs or one drug class
  causes of death  "Heart disease killed 700,000 Americans in 2022"
                   national, final counts 1999-2017 and provisional
                   counts for full years from 2020

The check reads one figure for one year. Rates per 100,000, subgroups
(age, race), trends and years NCHS has not put on data.cdc.gov (life
expectancy after 2020 nationally) return None, so the claim goes to search.
"""
import re
from dataclasses import dataclass, replace
from typing import Optional

from claims.verdict import HIGH, MEDIUM, Verdict
from config import logger
from evidence.datapoint import Geography
from evidence.excerpt import row as ex_row
from evidence.sources import SourceUnavailable, cdc
from evidence.units import PERSONS, UNKNOWN
from services.crime import figures, one_year, unnamed_place
from services.regulatory import fact, figure_check

_LE = re.compile(r"\blife expectancy\b|\blife span\b|\blifespan\b", re.I)
_OVERDOSE = re.compile(r"\boverdos(?:e|es|ed|ing)\b", re.I)
_DEATH = re.compile(r"\b(died|dies|die|deaths?|killed|kills|kill|fatalit(?:y|ies)|death toll|lives|lost)\b", re.I)
# drug word: (VSRR indicator, label, caveat)
_DRUGS = [
    (r"\bfentanyl\b", "Synthetic opioids, excl. methadone (T40.4)",
     "deaths involving synthetic opioids other than methadone",
     "CDC counts deaths involving synthetic opioids other than methadone, mostly illicit fentanyl; "
     "it publishes no fentanyl-only count here."),
    (r"\bsynthetic opioids?\b", "Synthetic opioids, excl. methadone (T40.4)",
     "deaths involving synthetic opioids other than methadone", ""),
    (r"\bheroin\b", "Heroin (T40.1)", "deaths involving heroin", ""),
    (r"\bcocaine\b", "Cocaine (T40.5)", "deaths involving cocaine", ""),
    (r"\bmeth(?:amphetamines?)?\b|\bpsychostimulants?\b", "Psychostimulants with abuse potential (T43.6)",
     "deaths involving psychostimulants with abuse potential",
     "CDC counts deaths involving psychostimulants with abuse potential, mostly methamphetamine."),
    (r"\bmethadone\b", "Methadone (T40.3)", "deaths involving methadone", ""),
    (r"\bopioids?\b", "Opioids (T40.0-T40.4,T40.6)", "deaths involving opioids", ""),
]
_CAUSES = [
    ("heart", r"\bheart disease\b", "heart disease"),
    ("cancer", r"\bcancers?\b", "cancer"),
    ("stroke", r"\bstrokes?\b", "stroke"),
    ("alzheimers", r"\balzheimer'?s?\b(?:\s+disease)?", "Alzheimer's disease"),
    ("diabetes", r"\bdiabetes\b", "diabetes"),
    ("suicide", r"\bsuicides?\b", "suicide"),
    ("accidents", r"\bunintentional injur(?:y|ies)\b|\baccidents?\b|\baccidental (?:deaths|injuries)\b",
     "unintentional injuries"),
    ("clrd", r"\bchronic lower respiratory diseases?\b", "chronic lower respiratory disease"),
    ("flu_pneumonia", r"\b(?:flu|influenza) and pneumonia\b", "influenza and pneumonia"),
    ("kidney", r"\bkidney disease\b|\bnephritis\b", "kidney disease"),
]
_ALL_DEATHS = re.compile(r"\b(?:total|all[- ]cause) (?:number of )?deaths\b|\bAmericans died (?:in|during) \d{4}\b|"
                         r"\bdeaths in the (?:U\.S\.|United States) (?:in|during) \d{4}\b", re.I)
# Not the series: a part of the population, a rate, a share, a trend, another place.
_OTHER = re.compile(
    r"\b(rates?|per 100,?000|per capita|age[- ]adjusted|percent|share|ages?|aged|adults?|children|kids|"
    r"teens?|teenagers?|youths?|infants?|babies|elderly|seniors|older|younger|(?:under|over) (?:the )?age|"
    r"\d+ and (?:over|older|up)|"
    r"Black|white|Hispanic|Latino|Asian|Native|race|racial|veterans?|counties|county|city|cities|"
    r"rural|urban|world|global\w*|countries|lung|breast|colon|colorectal|prostate|pancreatic|skin|liver|"
    r"ovarian|cervical|brain|blood|bladder|stomach|car|auto|traffic|motor vehicle|road|gun|firearm|"
    r"drowning|COVID\w*|coronavirus|pandemic|excess|healthy|at 65|at age|"
    r"rose|rise[sn]?|rising|fell|falls?|falling|fallen|increase[ds]?|decrease[ds]?|drop(?:ped|s)?|"
    r"declin\w+|jump\w*|surg\w+|spik\w+|soar\w*|plung\w+|doubl\w+|tripl\w+|highest|lowest|record|"
    r"since|compared|grew|growth|first half|so far|months?|weeks?|daily|a day|every)\b|%", re.I)
_MALE = re.compile(r"\b(men|males?|boys)\b", re.I)
_FEMALE = re.compile(r"\b(women|females?|girls)\b", re.I)


@dataclass
class Read:
    kind: str          # "life_expectancy" | "overdose" | "cause"
    year: int
    figure: str
    place: str         # "United States" or a state name
    state: str         # VSRR state code: "US" or the USPS abbreviation
    geography: Optional[Geography]
    sex: str = "Total"
    indicator: str = ""
    what: str = ""     # how the measure reads in a headline
    note: str = ""     # a caveat the measure carries
    cause: str = ""


def _place(parsed, text: str):
    from evidence.sources.fbi_crime import STATE_ABBR
    geo = getattr(parsed, "geography", None)
    if geo is None:
        return ("United States", "US", None) if unnamed_place(text) else None
    if geo.level == "nation":
        return "United States", "US", None
    if geo.level == "state" and geo.fips in STATE_ABBR:
        return geo.name, STATE_ABBR[geo.fips], geo
    return None


def read(text: str, parsed=None) -> Optional[Read]:
    le = bool(_LE.search(text))
    od = bool(_OVERDOSE.search(text))
    drugs = [d for d in _DRUGS if re.search(d[0], text, re.I)]
    if drugs and len(drugs) > 1:
        # "synthetic opioids" also says "opioids"; any other pair is two drugs
        names = {d[1] for d in drugs}
        if names == {"Synthetic opioids, excl. methadone (T40.4)", "Opioids (T40.0-T40.4,T40.6)"}:
            drugs = [d for d in drugs if d[1].startswith("Synthetic")][:1]
        else:
            return None
    causes = [c for c in _CAUSES if re.search(c[1], text, re.I)]
    kinds = [k for k, on in (("life_expectancy", le), ("overdose", od or (bool(drugs) and _DEATH.search(text))),
                             ("cause", bool(causes) or bool(_ALL_DEATHS.search(text)))) if on]
    if len(kinds) != 1:
        return None
    kind = kinds[0]
    year = one_year(text, parsed)
    if year is None:
        return None
    where = _place(parsed, text)
    if where is None:
        return None
    place, state, geo = where
    text = re.sub(r"\b(?:in |for )?the 12[- ]months? (?:ending|ended) (?:in )?December(?= \d{4})", " ", text, flags=re.I)
    scrub = text
    sex = "Total"
    if kind == "life_expectancy":
        male, female = bool(_MALE.search(text)), bool(_FEMALE.search(text))
        if male and female:
            return None
        sex = "Male" if male else "Female" if female else "Total"
        scrub = _MALE.sub(" ", _FEMALE.sub(" ", scrub))
        scrub = re.sub(r"\bat birth\b", " ", scrub, flags=re.I)
    if _OTHER.search(scrub):
        return None
    figs = figures(text)
    if len(figs) != 1:
        return None
    fig = figs[0].group(0).strip()
    if kind == "life_expectancy":
        try:
            v = float(fig.replace(",", ""))
        except ValueError:
            return None
        if not 40 <= v <= 100:
            return None
        return Read(kind=kind, year=year, figure=fig, place=place, state=state, geography=geo, sex=sex)
    if not _DEATH.search(text):
        return None
    if kind == "overdose":
        if drugs:
            _, indicator, what, note = drugs[0]
        else:
            indicator, what, note = cdc.ALL_DRUGS, "drug overdose deaths", ""
        return Read(kind=kind, year=year, figure=fig, place=place, state=state, geography=geo,
                    indicator=indicator, what=what, note=note)
    # causes of death: national only
    if state != "US":
        return None
    if len(causes) > 1:
        return None
    cause, what = (causes[0][0], causes[0][2]) if causes else ("all", "all causes")
    return Read(kind=kind, year=year, figure=fig, place=place, state=state, geography=geo,
                cause=cause, what=what)


def _with_place(dp, r: Read):
    return replace(dp, geography=r.geography) if r.geography is not None else dp


async def _life_expectancy(r: Read, text: str, parsed) -> Optional[Verdict]:
    d = await cdc.life_expectancy(r.year, r.sex, r.place)
    if not d:
        return None
    value = float(d["value"])
    shown = f"{value:.1f} years"
    who = {"Male": "males", "Female": "females"}.get(r.sex, "")
    label = "Life expectancy at birth" + (f", {who}" if who else "") + ("" if r.place == "United States" else f", {r.place}")
    dataset = ("NCHS, death rates and life expectancy at birth" if d["dataset"] == cdc.LE_HISTORIC
               else f"NCHS, U.S. state life expectancy by sex, {r.year}")
    dp = fact("CDC", label, shown, cdc.page(d["dataset"]), year=r.year, magnitude=value,
              basis=f"National Vital Statistics System, {dataset}", series_id=d["dataset"],
              rows=[ex_row(f"{label}, {r.year}", shown, cited=True)], units="years")
    v = figure_check(parsed, r.figure, _with_place(dp, r), text, UNKNOWN)
    if v is None:
        return None
    place = "U.S." if r.place == "United States" else f"{r.place}'s"
    sub = f" for {who}" if who else ""
    head = f"CDC National Center for Health Statistics: {place} life expectancy at birth{sub} was {shown} in {r.year}"
    head += "." if v.verdict == "Supported" else f"; the claim says {r.figure}."
    return replace(v, headline=head)


async def _overdose(r: Read, text: str, parsed) -> Optional[Verdict]:
    d = await cdc.overdose_deaths(r.year, r.indicator, r.state)
    if not d:
        return None
    reported, predicted = d.get("reported"), d.get("predicted")
    value = float(reported if reported is not None else predicted)
    shown = f"{int(value):,}"
    rows = [ex_row(f"{r.what.capitalize()}, 12 months ending December {r.year}", shown, cited=True)]
    if predicted is not None and reported is not None and predicted != reported:
        rows.append(ex_row("Predicted, allowing for deaths still under investigation", f"{int(predicted):,}"))
    where = "" if r.state == "US" else f", {d.get('state_name') or r.place}"
    dp = fact("CDC", f"{r.what.capitalize()}{where}", shown, cdc.page(cdc.OVERDOSE), year=r.year,
              magnitude=value, family=PERSONS,
              basis="National Vital Statistics System, provisional drug overdose death counts (VSRR)",
              series_id=f"{r.state}/{r.indicator}", rows=rows, units="deaths",
              note="Provisional counts; NCHS's final counts usually differ by 1-2%.")
    spread = abs((predicted or value) - value)
    dp.extra["margin_of_error"] = max(spread, 0.025 * value)
    dp.extra["caveat"] = ("These are NCHS provisional counts for the 12 months ending in December; final "
                          "counts usually differ by 1-2%, so a figure within that range counts as a match.")
    v = figure_check(parsed, r.figure, _with_place(dp, r), text, PERSONS)
    if v is None:
        return None
    place = "" if r.state == "US" else f" in {d.get('state_name') or r.place}"
    head = (f"CDC National Center for Health Statistics (provisional counts): there were {shown} "
            f"{r.what}{place} in the 12 months ending December {r.year}")
    head += "." if v.verdict == "Supported" else f"; the claim says {r.figure}."
    caveats = list(v.caveats) + ([r.note] if r.note else [])
    if r.indicator != cdc.ALL_DRUGS:
        caveats.append("A death involving several drugs is counted under each of them.")
    return replace(v, headline=head, caveats=caveats, confidence=MEDIUM)


async def _cause(r: Read, text: str, parsed) -> Optional[Verdict]:
    d = await cdc.cause_deaths(r.year, r.cause)
    if not d:
        return None
    value = float(d["deaths"])
    shown = f"{int(value):,}"
    final = bool(d.get("final"))
    label = "Deaths, all causes" if r.cause == "all" else f"Deaths from {r.what}"
    basis = ("National Vital Statistics System, leading causes of death (final)" if final else
             "National Vital Statistics System, monthly provisional counts of deaths by select causes")
    dp = fact("CDC", label, shown, cdc.page(d["dataset"]), year=r.year, magnitude=value, family=PERSONS,
              basis=basis, series_id=f"{d['dataset']}/{r.cause}",
              rows=[ex_row(f"{label}, United States, {r.year}", shown, cited=True)], units="deaths",
              note="" if final else "The sum of 12 monthly provisional counts, by place of death.")
    if not final:
        dp.extra["margin_of_error"] = 0.015 * value
        dp.extra["caveat"] = ("Provisional counts summed over the year; NCHS's final count differs slightly, "
                              "so a figure within 1.5% counts as a match.")
    v = figure_check(parsed, r.figure, dp, text, PERSONS)
    if v is None:
        return None
    kind = "final" if final else "provisional"
    what = "deaths from all causes" if r.cause == "all" else f"deaths from {r.what}"
    head = f"CDC National Center for Health Statistics ({kind} counts): there were {shown} {what} in the U.S. in {r.year}"
    head += "." if v.verdict == "Supported" else f"; the claim says {r.figure}."
    return replace(v, headline=head, confidence=HIGH if final and not v.caveats else MEDIUM)


async def check(text: str, parsed) -> Optional[Verdict]:
    r = read(text, parsed)
    if r is None:
        return None
    run = {"life_expectancy": _life_expectancy, "overdose": _overdose, "cause": _cause}[r.kind]
    try:
        return await run(r, text, parsed)
    except SourceUnavailable as e:
        logger.warning("Health check skipped for %r: %s", text[:80], e)
        return None
    except Exception:  # noqa: BLE001
        logger.exception("Health check failed for %r", text[:120])
        return None


SEARCH_HINT = (
    "The claim is about U.S. vital statistics. Check CDC's National Center for Health "
    "Statistics: the Mortality in the United States data brief for life expectancy and "
    "deaths by cause (final data, by year), and the Vital Statistics Rapid Release "
    "provisional drug overdose death counts (12 months ending in a month). Say whether "
    "a figure is provisional or final, and name the year.")
