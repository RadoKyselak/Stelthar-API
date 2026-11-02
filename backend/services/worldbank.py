"""Figures about countries other than the United States, checked against the
World Bank's World Development Indicators (api.worldbank.org, free, no key).

  "Germany's unemployment rate was 3% in 2023"        SL.UEM.TOTL.ZS
  "India's population passed 1.4 billion in 2023"     SP.POP.TOTL
  "Japan's GDP grew 1.9% in 2023"                     NY.GDP.MKTP.KD.ZG

The claim must name one country (by its common name: "the UK", "South
Korea", "Russia", "Türkiye") and one of a few measures written plainly; the
figure is read from the World Bank for the claim's year, or its latest year
when the claim names none (the answer says which). Anything else is left to
the next step: a city or region inside a country, two countries, a subgroup
("youth unemployment"), a quarter or month, a figure in another currency or
at purchasing-power parity, a trend or a ranking.

Some World Bank figures are not the number a country's own statistics office
publishes: unemployment is the ILO's modeled estimate (harmonised across
countries) and inflation is the annual average change in consumer prices.
The answer says so, and a claim at odds with them is judged with Medium
confidence, since the claim may be quoting the national measure.

U.S. claims are not checked here: the U.S. agencies' own series answer them.
"""
import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from claims.verdict import CONTRADICTED, HIGH, MEDIUM, MOSTLY_ACCURATE, SUPPORTED, Verdict, assess
from config import logger
from evidence.datapoint import Geography
from evidence.excerpt import row as ex_row
from evidence.sources import SourceUnavailable, worldbank
from evidence.units import PERCENT, PERSONS, UNKNOWN, USD, humanize, humanize_claim, parse_quantity
from services.regulatory import NEGATION, fact

SOURCE = "World Bank"
DATASET = "World Development Indicators"


# ------------------------------------------------------------------ indicators

@dataclass(frozen=True)
class Indicator:
    code: str
    noun: str            # "unemployment rate", as it reads after "Germany's"
    family: str          # evidence.units family of the figure
    kind: str            # "percent" | "usd" | "count" | "plain": how the claim writes it
    decimals: int        # places shown
    unit: str = ""       # after a plain number: "years", "births per woman"
    qualifier: str = ""  # shown after the year: "(ILO modeled estimate)"
    note: str = ""       # how the World Bank measures it, for the reader
    soft: bool = False   # a national figure can differ: a mismatch is Medium confidence


_UNEMP = Indicator(
    "SL.UEM.TOTL.ZS", "unemployment rate", PERCENT, "percent", 1, qualifier="(ILO modeled estimate)",
    note=("The World Bank publishes the International Labour Organization's modeled estimate, which "
          "uses the same definition for every country; a country's own statistics office (or its "
          "count of registered jobseekers) can report a different rate."),
    soft=True)
_INFL = Indicator(
    "FP.CPI.TOTL.ZG", "inflation rate", PERCENT, "percent", 1, qualifier="(annual average, consumer prices)",
    note=("This is the change in average consumer prices over the whole year, as the World Bank "
          "compiles it; the 12-month rate for a single month (often quoted in the news), or the "
          "national statistics office's own headline index, can differ."),
    soft=True)
_GROWTH = Indicator(
    "NY.GDP.MKTP.KD.ZG", "real GDP growth", PERCENT, "percent", 1, qualifier="(annual, constant prices)",
    note=("Annual growth of GDP at constant local-currency prices, from national accounts; national "
          "offices revise these figures, so an early estimate can differ."),
    soft=True)
_GDP = Indicator(
    "NY.GDP.MKTP.CD", "GDP", USD, "usd", 2, qualifier="(current US dollars)",
    note="GDP converted to U.S. dollars at each year's official exchange rate, so it moves with the currency.")
_GDP_PC = Indicator(
    "NY.GDP.PCAP.CD", "GDP per capita", USD, "usd", 0, qualifier="(current US dollars)",
    note="GDP per person, converted to U.S. dollars at each year's official exchange rate.")
_POP = Indicator(
    "SP.POP.TOTL", "population", PERSONS, "count", 2, qualifier="(midyear estimate)",
    note="Midyear population estimate, counting all residents regardless of citizenship.")
_LIFE = Indicator(
    "SP.DYN.LE00.IN", "life expectancy at birth", UNKNOWN, "plain", 1, unit="years")
_FERT = Indicator(
    "SP.DYN.TFRT.IN", "fertility rate", UNKNOWN, "plain", 2, unit="births per woman")
_INFANT = Indicator(
    "SP.DYN.IMRT.IN", "infant mortality rate", UNKNOWN, "plain", 1, unit="deaths per 1,000 live births")
_MIL = Indicator(
    "MS.MIL.XPND.GD.ZS", "military spending", PERCENT, "percent", 1, unit="of GDP", qualifier="(SIPRI estimate)",
    note="Military expenditure as a share of GDP, from the Stockholm International Peace Research Institute.")

INDICATORS = (_UNEMP, _INFL, _GROWTH, _GDP, _GDP_PC, _POP, _LIFE, _FERT, _INFANT, _MIL)

_OF_GDP = r"(?:%|percent|per cent)\s+of\s+(?:(?:its|the(?: country'?s)?|national)\s+)?(?:GDP|gross domestic product|economic output|output)"


def _indicator(text: str) -> Optional[Indicator]:
    """The one measure a claim is about, or None when it is not plainly one."""
    t = text
    if re.search(r"\b(PPP|purchasing[- ]power|GNI|GNP|national income|per capita income|income per (?:capita|person))\b", t, re.I):
        return None
    if re.search(r"\b(military|defen[cs]e)\s+(?:spending|expenditures?|budget|outlays?)\b|"
                 r"\b(?:spen[dt]s?|spending)\b[^.;]{0,50}\b(?:on\s+)?(?:its\s+|the\s+)?(?:military|defen[cs]e|armed forces)\b",
                 t, re.I):
        return _MIL if re.search(_OF_GDP, t, re.I) else None
    if re.search(r"\b(debt|deficit|exports?|imports?|trade|tax(?:es)?|revenue|investment|FDI|savings|"
                 r"health spending|education spending|R&D|research)\b", t, re.I):
        return None  # "% of GDP" and dollar figures for other measures
    if re.search(r"\bGDP per (?:capita|person|head)\b|\bper[- ]capita GDP\b|\bGDP per capita\b", t, re.I):
        return _GDP_PC
    if re.search(r"\blife expectancy\b", t, re.I):
        return _LIFE
    if re.search(r"\b(?:total\s+)?fertility rate\b|\b(?:births|children|babies|kids) per woman\b", t, re.I):
        return _FERT
    if re.search(r"\binfant mortality\b|\binfant deaths? per 1,?000\b", t, re.I):
        return _INFANT
    if re.search(r"\b(unemployment|jobless)(?:\s+rate)?\b", t, re.I):
        return _UNEMP
    if re.search(r"\binflation\b|\bconsumer prices?\b|\bCPI\b", t, re.I):
        return _INFL
    gdp = re.search(r"\b(GDP|gross domestic product|economy|economic output)\b", t, re.I)
    if gdp:
        if re.search(r"\b(grew|grow|grows|growing|growth|expand(?:ed|s)?|expansion|shr[au]nk|shrinks?|"
                     r"contract(?:ed|s|ion)?|declin(?:ed|es)|fell|falls?|rose|rises?|increased|decreased)\b", t, re.I):
            return _GROWTH
        return _GDP
    if _POP_SHAPE.search(t) and not _POP_BLOCK.search(t):
        return _POP
    return None


# Qualifiers the national total cannot answer.
_SUBGROUP = re.compile(
    r"\b(youth|young (?:people|adults|workers|men|women)|teens?|teenagers?|women|men|female|male|"
    r"girls?|boys?|elderly|seniors|older|retirees|migrants?|immigrants?|foreign(?:ers|[- ]born)?|"
    r"rural|urban|long[- ]term|working[- ]age|aged?\s+\d+|ages\s+\d+|over\s+\d{2}\b(?![.,]?\d*\s*(?:%|percent|million|billion|years))|"
    r"under[- ](?:5|five)|child mortality|neonatal|maternal|healthy life|at (?:age )?65|"
    r"core|food (?:inflation|prices)|energy (?:inflation|prices)|producer|wholesale|PPI|house prices|"
    r"rent|wages?|nominal GDP growth|density|growth rate of (?:the )?population|population growth)\b",
    re.I)
_PERIOD = re.compile(
    r"\b(Q[1-4]|quarter(?:ly)?|first half|second half|H[12]|month(?:ly)?|annuali[sz]ed|"
    r"fiscal|FY\s?\d{2,4}|year[- ]on[- ]year in|"
    r"January|February|March|April|June|July|August|September|October|November|December|"
    r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)\b", re.I)
_TREND = re.compile(
    r"\b(since|from\s+[\d$]|between|doubled|tripled|halved|record|highest|lowest|largest|smallest|biggest|"
    r"fastest|slowest|(?<!at )most|(?<!at )least|rank(?:s|ed)?|second|third|top \d+|times (?:larger|bigger|higher)|"
    r"than (?:the |that of |in )?[A-Z]|compared|versus|vs\.?|forecast|projected|expected|will|would|"
    r"could|by 20\d\d|per year|a year|each year|average(?:d)? over|decade)\b", re.I)
_OTHER_CURRENCY = re.compile(r"[€£¥₹₩₽]|\b(euros?|pounds?|sterling|yen|yuan|renminbi|RMB|rupees?|won|roubles?|"
                             r"rubles?|lira|francs?|pesos?|reais|real|krona|kronor|krone|zloty|rand|naira|"
                             r"local currency)\b|\b(?:C|A|CA|AU|NZ|HK|S)\$", re.I)

_USD_WORDS = re.compile(r"\bU\.?S\.?\s?(?:\$|dollars?\b)|\bUSD\b|\bUS\$", re.I)
_POP_SHAPE = re.compile(
    r"\bpopulation\b|\bpopulous\b|\b(?:has|have|had|is home to|was home to|home to)\b[^.;]{0,40}?"
    r"\b(?:people|residents|inhabitants)\b|\b(?:people|residents|inhabitants) live(?:d)? in\b", re.I)
_POP_BLOCK = re.compile(
    r"\b(lost|lose|died|deaths?|killed|fled|refugees?|employ\w*|work(?:ers|ing)?|jobs?|poverty|poor|"
    r"visit(?:ed|ors)?|tourists?|vaccinated|infected|cases|voters?|voted|users?|speak\w*|internet|"
    r"millionaires|homeless|hungry|malnourished|access|literate|smok\w*|obes\w*|diabet\w*)\b", re.I)
_YEAR = re.compile(r"\b(19[6-9]\d|20[0-9]\d)\b")


# ------------------------------------------------------------------ countries

# Common names the World Bank list does not spell this way. Names, not data.
_ALIASES: Dict[str, Tuple[str, ...]] = {
    "GBR": ("UK", "U.K.", "Britain", "Great Britain", "United Kingdom"),
    "KOR": ("South Korea", "Republic of Korea", "Korea, Rep."),
    "PRK": ("North Korea",),
    "RUS": ("Russia", "Russian Federation"),
    "CZE": ("Czech Republic", "Czechia"),
    "TUR": ("Turkey", "Türkiye", "Turkiye"),
    "SVK": ("Slovakia", "Slovak Republic"),
    "KGZ": ("Kyrgyzstan",),
    "LAO": ("Laos",),
    "SYR": ("Syria",),
    "VNM": ("Vietnam", "Viet Nam"),
    "COD": ("DR Congo", "DRC", "Democratic Republic of the Congo", "Democratic Republic of Congo",
            "Congo-Kinshasa"),
    "COG": ("Republic of the Congo", "Congo-Brazzaville"),
    "CIV": ("Ivory Coast", "Côte d'Ivoire", "Cote d'Ivoire"),
    "BRN": ("Brunei",),
    "CPV": ("Cape Verde",),
    "SWZ": ("Swaziland",),
    "MMR": ("Burma", "Myanmar"),
    "NLD": ("Holland", "the Netherlands"),
    "ARE": ("UAE", "U.A.E.", "the Emirates"),
    "MKD": ("Macedonia",),
    "TLS": ("East Timor",),
    "PSE": ("Palestine", "Palestinian territories", "West Bank and Gaza"),
    "NRU": ("Nauru",),
    "HKG": ("Hong Kong",),
    "MAC": ("Macau", "Macao"),
    "KNA": ("Saint Kitts and Nevis",),
    "LCA": ("Saint Lucia",),
    "VCT": ("Saint Vincent and the Grenadines",),
    "FSM": ("Micronesia",),
    "BHS": ("the Bahamas", "Bahamas"),
    "GMB": ("the Gambia", "Gambia"),
    "PHL": ("the Philippines", "Philippines"),
}
# How a country reads in a sentence, where the World Bank's list name does not.
_DISPLAY = {
    "GBR": "the UK", "KOR": "South Korea", "PRK": "North Korea", "RUS": "Russia", "SVK": "Slovakia",
    "KGZ": "Kyrgyzstan", "LAO": "Laos", "SYR": "Syria", "VNM": "Vietnam", "COD": "the DR Congo",
    "COG": "the Republic of the Congo", "CIV": "Côte d'Ivoire", "BRN": "Brunei", "EGY": "Egypt",
    "IRN": "Iran", "VEN": "Venezuela", "YEM": "Yemen", "HKG": "Hong Kong", "MAC": "Macao",
    "NLD": "the Netherlands", "ARE": "the UAE", "PHL": "the Philippines", "TUR": "Türkiye",
    "NRU": "Nauru", "FSM": "Micronesia", "BHS": "the Bahamas", "GMB": "the Gambia",
    "SOM": "Somalia", "CZE": "Czechia",
}
# U.S. (the U.S. series answer these) and names that are also ordinary words,
# people or U.S. states ("Georgia").
_SKIP = {"USA", "PRI", "GUM", "VIR", "ASM", "MNP", "GEO"}
# Ordinary words when lower-case: matched only as written with a capital.
_CASE_SENSITIVE = {"turkey", "chad", "jordan", "china", "japan", "guinea", "niger", "georgia", "panama",
                   "jersey", "dominica", "togo", "mali", "oman", "peru", "cuba"}
# Parts of a country: a figure for the whole country does not answer them.
_SUBNATIONAL = re.compile(
    r"\b(England|Scotland|Wales|Northern Ireland|Bavaria|Catalonia|Quebec|Ontario|Alberta|British Columbia|"
    r"Tokyo|Osaka|Shanghai|Beijing|Guangdong|Mumbai|Delhi|Kerala|Maharashtra|"
    r"Uttar Pradesh|Tamil Nadu|Sao Paulo|São Paulo|Rio|Lombardy|Sicily|Andalusia|Flanders|Wallonia|"
    r"Siberia|Moscow|Kyiv|Kiev|Istanbul|Dubai|Abu Dhabi|Gaza Strip|West Bank|Mainland|Taiwan)\b"
    r"|\b(province|provinces|provincial|prefecture|state of|states? in|regional|region|regions|"
    r"city|cities|capital|municipal(?:ity)?|county|district|metro(?:politan)?|town|village|island of)\b",
    re.I)
# Wider places that are not one country.
_SUPRANATIONAL = re.compile(
    r"\b(world(?! Bank)|global(?:ly)?|worldwide|EU|European Union|euro ?area|eurozone|OECD|G7|G20|BRICS|"
    r"Europe|Asia|(?<!South )Africa|Latin America|the Middle East|Scandinavia|Nordic|ASEAN)\b", re.I)
_US = re.compile(r"\b(United States|U\.S\.A?\.?|USA|America|Americans?|US)\b")


def _fold(s: str) -> str:
    """Accents removed, so "Türkiye" and "Turkiye", "Côte" and "Cote" match."""
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _names(countries: List[Dict[str, str]]) -> List[Tuple[str, str]]:
    """[(name as written, iso3)], longest first, from the World Bank's list
    plus the common names above. A short name two entries share ("Congo",
    "Korea") is left out: it does not say which."""
    by_iso = {c["iso3"]: c for c in countries}
    out: Dict[str, str] = {}
    shared: Dict[str, set] = {}
    for c in countries:
        iso = c["iso3"]
        if iso in _SKIP:
            continue
        name = re.sub(r"\s*\(.*?\)\s*", " ", c["name"]).strip()
        cands = [name]
        if ", " in name:
            head, tail = name.split(", ", 1)
            cands.append(head)
            if tail.lower() == "the":
                cands.append(f"the {head}")
        for n in cands:
            shared.setdefault(n.lower(), set()).add(iso)
    for n, isos in shared.items():
        if len(isos) == 1:
            out[n] = next(iter(isos))
    for iso, extra in _ALIASES.items():
        if iso in by_iso and iso not in _SKIP:
            for n in extra:
                out[n.lower()] = iso
    return sorted(out.items(), key=lambda kv: -len(kv[0]))


def _find_countries(text: str, names: List[Tuple[str, str]]) -> List[Tuple[str, re.Match]]:
    """Every country the text names: [(iso3, match)], each once."""
    folded = _fold(text)
    low = folded.lower()
    taken: List[Tuple[int, int]] = []
    found: List[Tuple[str, re.Match]] = []
    for name, iso in names:
        pat = re.escape(_fold(name).lower())
        for m in re.finditer(rf"(?<![\w.])(?:the\s+)?{pat}(?![\w])", low):
            if any(a < m.end() and m.start() < b for a, b in taken):
                continue
            core = folded[m.end() - len(_fold(name)):m.end()]
            if name in _CASE_SENSITIVE and not core[:1].isupper():
                continue
            # "U.K." ends in a dot the lookahead cannot see; plain "UK"/"US" must be upper-case.
            if name in ("uk", "drc", "uae") and core != core.upper():
                continue
            taken.append((m.start(), m.end()))
            if iso not in [f[0] for f in found]:
                found.append((iso, m))
    return found


def _display_name(c: Dict[str, str]) -> str:
    if c["iso3"] in _DISPLAY:
        return _DISPLAY[c["iso3"]]
    return re.sub(r"\s*\(.*?\)\s*", " ", c["name"]).strip()


def _possessive(name: str) -> str:
    base = name[0].upper() + name[1:]
    return f"{base}'" if base.endswith("s") and not base.endswith("ss") else f"{base}'s"


# ------------------------------------------------------------------ the figure

_HEDGE = r"(?:about|approximately|approx\.?|roughly|around|nearly|almost|close to|some|circa|~)"
_GREATER = r"(?:more than|over|above|exceed(?:ed|s|ing)?|topped|tops|passed|passes|surpassed|surpasses|crossed|" \
           r"higher than|greater than|north of|in excess of|beyond)"
_LESS = r"(?:less than|under|below|fewer than|lower than|beneath|short of)"
_AT_LEAST = r"(?:at least|no less than|no fewer than)"
_AT_MOST = r"(?:at most|no more than|not more than)"
_LEAD = rf"(?:(?P<cmp>{_AT_LEAST}|{_AT_MOST}|{_GREATER}|{_LESS})\s+)?(?:(?P<hedge>{_HEDGE})\s+)?"
_SCALE = r"(?:trillion|billion|million|thousand|tn|bn|mn|m|k)\b\.?"

_FIGURES = {
    "percent": re.compile(_LEAD + r"(?P<neg>-|minus\s+|negative\s+)?(?P<num>\d+(?:\.\d+)?)\s*(?:%|percent\b|per cent\b)", re.I),
    "usd": re.compile(_LEAD + r"(?P<num>(?:US\s?\$|USD\s?|\$)\s?\d[\d,]*(?:\.\d+)?\s*(?:" + _SCALE + r")?"
                      r"|\d[\d,]*(?:\.\d+)?\s*(?:" + _SCALE + r")?\s*(?:U\.?S\.? )?dollars\b)", re.I),
    "count": re.compile(_LEAD + r"(?<![\d.,])(?P<num>\d[\d,]*(?:\.\d+)?\s*(?:" + _SCALE + r")?)(?!\s*(?:%|percent))", re.I),
    "plain": re.compile(_LEAD + r"(?<![\d.,])(?P<num>\d+(?:\.\d+)?)(?![\d,]|\s*(?:%|percent|million|billion))", re.I),
}
_COMPARATOR = (("greater than", _GREATER), ("at least", _AT_LEAST), ("less than", _LESS), ("at most", _AT_MOST))
_TAIL_CMP = re.compile(r"^\s*(or more|or higher|or above|plus|\+)", re.I), re.compile(r"^\s*(or less|or fewer|or lower)", re.I)


@dataclass
class Figure:
    text: str
    value: float
    comparator: str
    quantity: object


def _figure(text: str, ind: Indicator) -> Optional[Figure]:
    """The claim's figure for this measure, with its comparator, or None when
    there is none or more than one."""
    rx = _FIGURES[ind.kind]
    hits = []
    for m in rx.finditer(text):
        num = m.group("num")
        bare = re.sub(r"[^\d.]", "", num.split()[0] if ind.kind != "usd" else num)
        if ind.kind in ("count", "plain") and _YEAR.fullmatch(bare or "") and not re.search(_SCALE, num, re.I):
            continue  # a year, not the figure
        if ind.kind == "plain" and re.match(r"\s*,?\s*000\b", text[m.end():]):
            continue
        if ind.kind == "plain" and re.search(r"per\s+$", text[max(0, m.start("num") - 6):m.start("num")], re.I):
            continue  # "per 1,000 live births"
        hits.append(m)
    if ind.kind == "count":
        hits = [m for m in hits if re.search(_SCALE, m.group("num"), re.I) or len(re.sub(r"\D", "", m.group("num"))) >= 6]
    if len(hits) != 1:
        return None
    m = hits[0]
    shown = (f"{m.group('hedge')} " if m.group("hedge") else "") + m.group("num")
    q = parse_quantity(shown)
    if q is None:
        return None
    q = replace(q, family=ind.family)
    if ind.kind == "percent" and (m.group("neg") or "").strip():
        q = replace(q, magnitude=-abs(q.magnitude))
    comp = "equals"
    c = m.group("cmp")
    if c:
        comp = next(name for name, pat in _COMPARATOR if re.fullmatch(pat, c, re.I))
    elif _TAIL_CMP[0].match(text[m.end():]):
        comp = "at least"
    elif _TAIL_CMP[1].match(text[m.end():]):
        comp = "at most"
    if ind is _GROWTH and q.magnitude > 0 and re.search(
            r"\b(shr[au]nk|shrinks?|contract(?:ed|s|ion)?|declin(?:ed|es)|fell|falls?|decreased|"
            r"recession of)\b", text, re.I):
        q = replace(q, magnitude=-q.magnitude)
        comp = {"greater than": "less than", "less than": "greater than",
                "at least": "at most", "at most": "at least"}.get(comp, comp)
    return Figure(text=m.group(0).strip(), value=q.magnitude, comparator=comp, quantity=q)


def _fmt(value: float, ind: Indicator) -> str:
    if ind.family == PERCENT:
        return f"{value:.{ind.decimals}f}% {ind.unit}".strip()
    if ind.family == USD:
        if abs(value) >= 1e9:
            return humanize(value, USD)
        return f"${value:,.0f}"
    if ind.family == PERSONS:
        return humanize(value, PERSONS) if abs(value) >= 1e6 else f"{value:,.0f}"
    return f"{value:.{ind.decimals}f} {ind.unit}".strip()


def _claimed(fig: Figure, ind: Indicator) -> str:
    if ind is _GROWTH:
        cmp = {"greater than": "more than ", "at least": "at least ", "less than": "less than ",
               "at most": "at most "}
        if fig.value < 0:
            # The comparator was flipped with the sign: "shrank more than 1%" is "less than -1%".
            flip = {"less than": "greater than", "greater than": "less than", "at most": "at least",
                    "at least": "at most"}.get(fig.comparator, fig.comparator)
            return f"it shrank {cmp.get(flip, '')}{humanize_claim(-fig.value, PERCENT, fig.quantity.precision)}"
        return f"it grew {cmp.get(fig.comparator, '')}{humanize_claim(fig.value, PERCENT, fig.quantity.precision)}"
    phrase = {"greater than": "more than ", "at least": "at least ", "less than": "less than ",
              "at most": "at most "}.get(fig.comparator, "")
    shown = humanize_claim(fig.value, ind.family, fig.quantity.precision)
    if ind.unit:
        shown = f"{shown} {ind.unit}"
    return phrase + shown


def _units_label(ind: Indicator) -> str:
    if ind.family == PERCENT:
        return f"percent {ind.unit}".strip()
    return ind.unit or {USD: "current US dollars", PERSONS: "people"}.get(ind.family, "")


def _url(code: str, iso2: str) -> str:
    return f"https://data.worldbank.org/indicator/{code}?locations={iso2}"


# ------------------------------------------------------------------ the check

def _blocked(text: str) -> bool:
    """A claim the national annual total cannot settle."""
    return bool(_SUBGROUP.search(text) or _PERIOD.search(text) or _TREND.search(text)
                or _SUPRANATIONAL.search(text) or _SUBNATIONAL.search(text))


async def check(text: str, parsed) -> Optional[Verdict]:
    """A figure about one non-U.S. country, checked against the World Bank,
    or None when the claim is not one this can settle."""
    try:
        return await _check(text or "", parsed)
    except SourceUnavailable as e:
        logger.warning("World Bank check skipped for %r: %s", (text or "")[:80], e)
        return None
    except Exception:  # noqa: BLE001 — a free check that fails leaves the claim to the next step
        logger.exception("World Bank check failed for %r", (text or "")[:120])
        return None


async def _check(text: str, parsed) -> Optional[Verdict]:
    ind = _indicator(text)
    if ind is None or _US.search(_USD_WORDS.sub(" ", text)) or _blocked(text) or NEGATION.search(text):
        return None
    if ind.kind == "usd" and _OTHER_CURRENCY.search(text):
        return None
    years = sorted({int(y) for y in _YEAR.findall(text)})
    if len(years) > 1:
        return None
    fig = _figure(text, ind)
    if fig is None:
        return None
    geo = _geography(text)
    if geo is not None and geo.level not in ("nation", "unknown"):
        return None  # a U.S. state, county, city or ZIP: not ours

    countries = await worldbank.countries()
    if not countries:
        return None
    found = _find_countries(text, _names(countries))
    if len(found) != 1:
        return None
    iso3, _ = found[0]
    country = next(c for c in countries if c["iso3"] == iso3)

    obs = await worldbank.indicator(iso3, ind.code)
    if not obs:
        return None
    year = years[0] if years else None
    this_year = datetime.now(timezone.utc).year
    if year is not None and (year > this_year or year < 1960):
        return None
    row = obs[0] if year is None else next((o for o in obs if o["year"] == year), None)
    if row is None:
        return None  # not published for that year (yet): the next step can look further
    return _judge(text, parsed, ind, country, row, obs, fig, assumed_year=year is None)


def _geography(text: str):
    try:
        from claims.geography import resolve_geography
        return resolve_geography(text)
    except Exception:  # noqa: BLE001
        return None


def _judge(text: str, parsed, ind: Indicator, country: Dict[str, str], row: Dict, obs: List[Dict],
           fig: Figure, assumed_year: bool) -> Optional[Verdict]:
    year, value = row["year"], row["value"]
    name = _display_name(country)
    official = _fmt(value, ind)
    label = f"{_possessive(name)} {ind.noun}"
    rows = [ex_row("Indicator", f"{row['indicator']} ({ind.code})"),
            ex_row("Country", country["name"])]
    rows += [ex_row(str(o["year"]), _fmt(o["value"], ind), cited=o["year"] == year)
             for o in sorted(obs, key=lambda o: o["year"]) if abs(o["year"] - year) <= 2]
    dp = fact(SOURCE, f"{label}, {year}", official, _url(ind.code, country["iso2"]), year=year,
              basis=DATASET, series_id=ind.code, rows=rows, magnitude=value, family=ind.family,
              note=ind.note, units=_units_label(ind))
    dp = replace(dp, metric="world_development_indicator",
                 geography=Geography(name=country["name"], level="nation", fips=country["iso3"]),
                 as_of=row.get("last_updated") or None, raw_value=repr(value))
    if parsed is None:
        from claims.parser import parse_claim
        parsed = parse_claim(text)
    pc = replace(parsed, metric=None, year=year, month=None, quarter=None, geography=None,
                 claimed_value=fig.quantity, comparator=fig.comparator, all_values=[], all_years=[year],
                 has_unresolved_period=False)
    v = assess(pc, [dp])
    if v.verdict not in (SUPPORTED, MOSTLY_ACCURATE, CONTRADICTED):
        return None

    claimed_s = _claimed(fig, ind)
    when = f"in {year}"
    verb = "grew" if ind is _GROWTH and value >= 0 else ("shrank" if ind is _GROWTH else "was")
    shown = official if ind is not _GROWTH else _fmt(abs(value), ind)
    if ind is _GROWTH:
        claim_line = f"{_possessive(name)} economy {verb} {shown} {when}"
        if ind.qualifier:
            claim_line += f" {ind.qualifier}"
        head = f"World Bank ({DATASET}): {claim_line[0].upper() + claim_line[1:]}"
    else:
        qual = f" {ind.qualifier}" if ind.qualifier else ""
        head = f"World Bank ({DATASET}): {label} {verb} {official} {when}{qual}"
    if v.verdict == SUPPORTED:
        head += "."
    elif v.verdict == MOSTLY_ACCURATE and v.gap_display:
        gap = v.gap_display
        if ind.unit and re.fullmatch(r"[\d.,]+", gap):
            # A plain-number gap reads in the measure's own unit and precision.
            gap = f"{float(gap.replace(',', '')):.1f} {ind.unit}".replace(".0 ", " ")
        head += f"; the claim says {claimed_s}, off by {gap}."
    else:
        head += f"; the claim says {claimed_s}."

    caveats: List[str] = []
    if ind.note:
        caveats.append(ind.note)
    if assumed_year:
        caveats.append(f"The claim names no year; the World Bank's latest figure ({year}) is used.")
    confidence = HIGH
    if v.verdict != SUPPORTED and (ind.soft or assumed_year):
        confidence = MEDIUM
    return replace(v, headline=head, confidence=confidence, caveats=caveats,
                   claimed_display=claimed_s, official_display=f"{official} ({year})")
