"""Resolve the place a claim is about — or decline to.

The previous resolver matched state names as bare substrings, so any text
*containing* a state name resolved to that state:

    "New York City"   -> New York State   (8.3M answered with 19.5M)
    "Kansas City"     -> Kansas           (the city is mostly in Missouri)
    "Virginia Beach"  -> Virginia
    "Oklahoma City"   -> Oklahoma

Nothing flagged the substitution, so a population claim about a city was
answered with a state figure two to three times larger and the verdict flipped.
Worse, it was phrasing-dependent: "Kansas City, Missouri" resolved correctly,
so the same city produced different verdicts depending on how it was written.

The rule here is that declining to resolve is a valid, useful answer. A claim
about a city returns a `place`-level geography, which no wired-up series can
satisfy, so the claim routes to grounded search — which can actually answer it —
instead of being answered against the wrong jurisdiction.

The same rule has to cover places that are not in the United States at all.
Returning None for those meant "no geography constraint", so every series here
being US-only was never checked against them:

    "The unemployment rate in France was 4% in 2023."   -> Supported  (US 3.6%)
    "The unemployment rate in Germany was 3% in 2023."  -> Contradicted (US 3.6%)

A named-but-unresolvable place therefore returns `Geography.unknown(...)`, which
fails closed downstream, and is kept distinct from None — "the claim named no
place", which legitimately means the series' own coverage applies.
"""
import re
from typing import Optional

from evidence.datapoint import Geography

_STATE_FIPS = {
    "alabama": "01", "alaska": "02", "arizona": "04", "arkansas": "05",
    "california": "06", "colorado": "08", "connecticut": "09", "delaware": "10",
    "district of columbia": "11", "florida": "12", "georgia": "13", "hawaii": "15",
    "idaho": "16", "illinois": "17", "indiana": "18", "iowa": "19", "kansas": "20",
    "kentucky": "21", "louisiana": "22", "maine": "23", "maryland": "24",
    "massachusetts": "25", "michigan": "26", "minnesota": "27", "mississippi": "28",
    "missouri": "29", "montana": "30", "nebraska": "31", "nevada": "32",
    "new hampshire": "33", "new jersey": "34", "new mexico": "35", "new york": "36",
    "north carolina": "37", "north dakota": "38", "ohio": "39", "oklahoma": "40",
    "oregon": "41", "pennsylvania": "42", "rhode island": "44",
    "south carolina": "45", "south dakota": "46", "tennessee": "47", "texas": "48",
    "utah": "49", "vermont": "50", "virginia": "51", "washington": "53",
    "west virginia": "54", "wisconsin": "55", "wyoming": "56", "puerto rico": "72",
}

_STATE_ABBREV = {
    "d.c.": "district of columbia", "dc": "district of columbia",
    "calif": "california", "mass": "massachusetts", "penn": "pennsylvania",
}

_NATIONAL_PHRASES = (
    "united states", "u.s.", "us", "usa", "america", "american",
    "nationally", "nationwide", "the country", "the nation", "federal",
)

# Words that, following or preceding a state name, mean the text is talking
# about a sub-state place rather than the state itself.
_PLACE_SUFFIXES = (
    "city", "county", "beach", "springs", "falls", "heights", "township",
    "village", "borough", "park", "valley", "harbor", "harbour", "island",
    "metro", "metropolitan area", "suburbs", "downtown",
)
_PLACE_PREFIXES = ("city of", "town of", "county of", "port of", "greater")

# Sorted longest-first so "west virginia" is tested before "virginia" and
# "new york" before "york"-adjacent noise.
_STATES_BY_LENGTH = sorted(_STATE_FIPS, key=len, reverse=True)

# Places that are definitively outside the coverage of every series wired up
# here. This is a lookup rather than a heuristic: the point is to name the
# common case exactly, so the response can say "France is not a U.S. geography"
# instead of the generic decline the fallback below produces.
_NON_US = frozenset("""
afghanistan albania algeria angola argentina armenia australia austria
azerbaijan bahrain bangladesh belarus belgium bolivia bosnia botswana brazil
bulgaria burma cambodia cameroon canada chad chile china colombia congo croatia
cuba cyprus czechia denmark ecuador egypt estonia ethiopia finland france gabon
germany ghana greece guatemala haiti honduras hungary iceland india indonesia
iran iraq ireland israel italy jamaica japan jordan kazakhstan kenya kuwait
kyrgyzstan laos latvia lebanon liberia libya lithuania luxembourg madagascar
malaysia mali malta mexico moldova mongolia morocco mozambique nepal netherlands
nicaragua niger nigeria norway oman pakistan panama paraguay peru philippines
poland portugal qatar romania russia rwanda senegal serbia singapore slovakia
slovenia somalia spain sudan sweden switzerland syria taiwan tajikistan tanzania
thailand tunisia turkey turkmenistan uganda ukraine uruguay uzbekistan venezuela
vietnam yemen zambia zimbabwe britain england scotland wales holland europe asia
africa scandinavia eurozone
""".split()) | frozenset([
    "ivory coast", "new zealand", "north korea", "south korea", "south africa",
    "saudi arabia", "sri lanka", "united arab emirates", "united kingdom",
    "great britain", "northern ireland", "european union", "south america",
    "latin america", "north america", "the middle east", "the caribbean",
    "uk", "u.k.", "uae", "u.a.e.",
])

_NON_US_BY_LENGTH = sorted(_NON_US, key=len, reverse=True)

# Months and other capitalised words that follow a locative preposition without
# naming a place. Without these the fallback would decline "in December".
_NOT_A_PLACE = frozenset("""
january february march april may june july august september october november
december q1 q2 q3 q4 congress medicare medicaid social security wall street
main street
""".split())

# "… in France", "… on Mars", "… across Europe". Capitalisation is the signal
# that a proper noun was named; the token is then checked against everything we
# can resolve, and anything left over is an unresolved place rather than a pass.
_LOCATIVE_RE = re.compile(
    r"\b(?:in|on|across|throughout|within)\s+(?:the\s+)?"
    r"([A-Z][A-Za-z.'-]*(?:\s+[A-Z][A-Za-z.'-]*){0,3})(?!\w)"
)


def _word_bounded(haystack: str, needle: str) -> Optional[re.Match]:
    return re.search(rf"(?<![a-z]){re.escape(needle)}(?![a-z])", haystack)


def looks_like_substate_place(text: str, state_name: str, match: re.Match) -> bool:
    """True when a state-name match is really part of a city or county name."""
    lowered = text.lower()

    # "<state> City", "<state> Beach", "<state> County", …
    tail = lowered[match.end():match.end() + 24].strip()
    for suffix in _PLACE_SUFFIXES:
        if re.match(rf"^{re.escape(suffix)}(?![a-z])", tail):
            return True

    # "City of <state>", "Greater <state>", …
    head = lowered[max(0, match.start() - 24):match.start()].strip()
    for prefix in _PLACE_PREFIXES:
        if head.endswith(prefix):
            return True

    return False


# "Washington, D.C." must win over the state of Washington. The state loop
# below would otherwise match "washington" first and answer a DC claim with
# Washington State's figures.
_DC_RE = re.compile(r"\bwashington,?\s+d\.?\s?c\b\.?|\bdistrict of columbia\b")

# Scopes that are explicitly not the United States, but are not countries
# either. Every structured series here is U.S.-only, so these must decline.
_SUPRANATIONAL = (
    "the world", "world", "worldwide", "global", "globally", "planet", "earth",
    "eu", "the eu", "euro area", "eurozone", "oecd", "g7", "g20",
    "developing countries", "emerging markets",
)

# A capitalised name directly before "'s" ("Chicago's population") or after a
# metric noun + "of" ("the population of Paris") names a place we must resolve
# or decline. Restricting "of" to metric nouns keeps "Bureau of Labor
# Statistics" and "Department of Defense" from being read as places.
_POSSESSIVE_RE = re.compile(r"\b([A-Z][A-Za-z.-]+(?:\s+[A-Z][A-Za-z.-]+){0,2})['’]s\b")
_METRIC_OF_RE = re.compile(
    r"\b(?i:population|gdp|economy|output|income|poverty(?: rate)?|unemployment(?: rate)?|"
    r"inflation(?: rate)?|residents|people|debt|wages|earnings|home values?|house prices)"
    r"\s+(?i:of)\s+(?:(?i:the)\s+)?([A-Z][A-Za-z.'-]*(?:\s+[A-Z][A-Za-z.'-]*){0,3})"
)
# Capitalised possessives that are not places. Declining them would only cost
# a structured answer, so the list covers the frequent cases, not every one.
_NOT_A_PLACE_POSSESSIVE = frozenset("""
fed federal reserve treasury congress senate house president administration
government bls bea census cbo omb irs america nation country
""".split())


def _proper_noun_is_unresolved(candidate: str) -> bool:
    c = " ".join(candidate.split()).lower().strip(".")
    if len(c) < 3 or c in _NOT_A_PLACE or c in _NOT_A_PLACE_POSSESSIVE:
        return False
    first = c.split()[0]
    if first in ("the", "this", "that", "its", "their", "his", "her"):
        return False
    return True


# People named for their state. Ambiguous ones are listed separately: "New
# Yorkers" may mean the city, "Washingtonians" D.C., "Georgians" the country.
_DEMONYMS = {
    "alabamian": "alabama", "alabaman": "alabama", "alaskan": "alaska", "arizonan": "arizona",
    "arkansan": "arkansas", "californian": "california", "coloradan": "colorado",
    "delawarean": "delaware", "floridian": "florida", "hawaii resident": "hawaii",
    "idahoan": "idaho", "illinoisan": "illinois", "hoosier": "indiana", "iowan": "iowa",
    "kansan": "kansas", "kentuckian": "kentucky", "louisianan": "louisiana",
    "louisianian": "louisiana", "mainer": "maine", "marylander": "maryland",
    "michigander": "michigan", "minnesotan": "minnesota", "mississippian": "mississippi",
    "missourian": "missouri", "montanan": "montana", "nebraskan": "nebraska",
    "nevadan": "nevada", "new hampshirite": "new hampshire", "new jerseyan": "new jersey",
    "new jerseyite": "new jersey", "new mexican": "new mexico",
    "north carolinian": "north carolina", "north dakotan": "north dakota",
    "ohioan": "ohio", "oklahoman": "oklahoma", "oregonian": "oregon",
    "pennsylvanian": "pennsylvania", "rhode islander": "rhode island",
    "south carolinian": "south carolina", "south dakotan": "south dakota",
    "tennessean": "tennessee", "texan": "texas", "utahn": "utah", "vermonter": "vermont",
    "virginian": "virginia", "west virginian": "west virginia",
    "wisconsinite": "wisconsin", "wyomingite": "wyoming",
}
_AMBIGUOUS_DEMONYMS = ("new yorker", "washingtonian", "georgian")
_DEMONYMS_BY_LENGTH = sorted(_DEMONYMS, key=len, reverse=True)
# "Cook County", "St. Louis County", "Orleans Parish": named before any state
# check, because "Washington County" is not Washington state.
_COUNTY_RE = re.compile(r"\b((?:[A-Z][\w.'\-]*\s){0,2}[A-Z][\w.'\-]*)\s+(County|Parish)\b")



# ------------------------------------------------------------------ local places
# A claim about a city, metro area or ZIP code is resolved to that place, never
# to the state or the nation around it. The names are kept as written; the
# Census place lists turn them into codes when a lookup needs one.

USPS = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas", "CA": "california",
    "CO": "colorado", "CT": "connecticut", "DE": "delaware", "DC": "district of columbia",
    "FL": "florida", "GA": "georgia", "HI": "hawaii", "ID": "idaho", "IL": "illinois",
    "IN": "indiana", "IA": "iowa", "KS": "kansas", "KY": "kentucky", "LA": "louisiana",
    "ME": "maine", "MD": "maryland", "MA": "massachusetts", "MI": "michigan", "MN": "minnesota",
    "MS": "mississippi", "MO": "missouri", "MT": "montana", "NE": "nebraska", "NV": "nevada",
    "NH": "new hampshire", "NJ": "new jersey", "NM": "new mexico", "NY": "new york",
    "NC": "north carolina", "ND": "north dakota", "OH": "ohio", "OK": "oklahoma", "OR": "oregon",
    "PA": "pennsylvania", "RI": "rhode island", "SC": "south carolina", "SD": "south dakota",
    "TN": "tennessee", "TX": "texas", "UT": "utah", "VT": "vermont", "VA": "virginia",
    "WA": "washington", "WV": "west virginia", "WI": "wisconsin", "WY": "wyoming",
    "PR": "puerto rico",
}

# Nicknames for cities, as people write them.
_CITY_NICKNAMES = {
    "nyc": "New York City", "n.y.c.": "New York City", "the big apple": "New York City",
    "la": "Los Angeles, California", "l.a.": "Los Angeles, California",
    "sf": "San Francisco, California", "s.f.": "San Francisco, California",
    "philly": "Philadelphia, Pennsylvania", "vegas": "Las Vegas, Nevada",
    "nola": "New Orleans, Louisiana", "chi-town": "Chicago, Illinois",
    "atl": "Atlanta, Georgia", "motor city": "Detroit, Michigan",
}
_NICKNAME_RE = re.compile(
    r"(?<!, )(?<![\w.])(NYC|N\.Y\.C\.|L\.A\.|LA|SF|S\.F\.|ATL)(?![\w.])"
    r"|\b(?i:the big apple|philly|vegas|nola|chi-town|motor city)\b")

# Regions that are metro areas, by the name of their main city.
_METRO_NICKNAMES = {
    "bay area": "San Francisco", "san francisco bay area": "San Francisco",
    "silicon valley": "San Jose", "twin cities": "Minneapolis", "dfw": "Dallas",
    "metroplex": "Dallas", "tri-state area": "New York", "tristate area": "New York",
    "inland empire": "Riverside", "research triangle": "Raleigh", "the triangle": "Raleigh",
    "hampton roads": "Virginia Beach", "puget sound": "Seattle", "south florida": "Miami",
    "the dmv": "Washington", "chicagoland": "Chicago", "the valley of the sun": "Phoenix",
}
_METRO_NICKNAME_RE = re.compile(
    r"\b(?i:(?:the\s+)?(?:san francisco\s+)?bay area|silicon valley|twin cities|metroplex|"
    r"tri-?state area|inland empire|research triangle|hampton roads|puget sound(?: region)?|"
    r"south florida|chicagoland|the valley of the sun|the dmv)\b|\bDFW\b")
_METRO_RE = re.compile(
    r"\b(?:(?i:greater)\s+([A-Z][\w.'’]*(?:[\s-]+[A-Z][\w.'’]*){0,3})"
    r"|([A-Z][\w.'’]*(?:[\s-]+[A-Z][\w.'’]*){0,4})\s+(?i:metro(?:politan)?(?:\s+(?:statistical\s+)?area|\s+region)?"
    r"|(?:area|region)(?!\s+(?:median|code))))\b")
_ZIP_RE = re.compile(
    r"\b(?i:zip(?:\s*code)?|zcta|postal\s+code)s?\s*#?\s*(\d{5})\b"
    r"|(?<![$\d,.])\b(?i:in|for)\s+(\d{5})(?![\d,.%]|\s*(?:people|residents|jobs|homes|dollars))")
_STATE_NAMES_RE = "|".join(re.escape(n.title()) for n in sorted(_STATE_FIPS, key=len, reverse=True))
_CITY_STATE_RE = re.compile(
    r"\b((?:[A-Z][\w.'’-]*\s+){0,3}[A-Z][\w.'’-]*),\s+"
    r"(" + _STATE_NAMES_RE + r"|[A-Z]{2}(?![\w])|[A-Z]\.[A-Z]\.)")
# Capitalised words that start a sentence or clause, not a place name.
_LEADING_NOT_PLACE = frozenset("""
in at for of the however meanwhile today overall nationally since now also and but
while last this even yet so from across like unlike than both by on as after before
during under over between according statewide still then thus indeed notably
""".split())


def _state_from(token: str) -> str:
    """"Texas", "TX" or "T.X." -> "texas"; "" when it is not a state."""
    t = token.replace(".", "").strip()
    if t.upper() in USPS and (len(t) == 2):
        return USPS[t.upper()]
    return t.lower() if t.lower() in _STATE_FIPS else ""


def _metro_name(raw: str) -> str:
    return " ".join(raw.replace("’", "'").split()).strip(" .,'")


def _local_place(text: str) -> Optional[Geography]:
    """A ZIP code, metro area, nicknamed city or "City, State", or None."""
    m = _ZIP_RE.search(text)
    if m:
        z = m.group(1) or m.group(2)
        return Geography(name=f"ZIP Code {z}", level="zcta", fips=z)

    m = _METRO_NICKNAME_RE.search(text)
    if m:
        key = re.sub(r"^the\s+", "", m.group(0).lower()) if m.group(0).lower() not in _METRO_NICKNAMES else m.group(0).lower()
        key = key.replace("tristate", "tri-state").replace("puget sound region", "puget sound")
        name = _METRO_NICKNAMES.get(key) or _METRO_NICKNAMES.get("the " + key) or _METRO_NICKNAMES.get(m.group(0).lower())
        if name:
            return Geography(name=name, level="metro")
    m = _METRO_RE.search(text)
    if m:
        name = _metro_name(m.group(1) or m.group(2))
        words = name.split()
        while words and words[0].lower() in _LEADING_NOT_PLACE:
            words = words[1:]
        name = " ".join(words)
        if re.sub(r"[.,\s]", "", name).lower() in ("dc", "washingtondc"):
            name = "Washington"
        low = name.lower()
        if name and low not in _NOT_A_PLACE and low not in _NON_US and not any(
                _word_bounded(low, p) for p in _NATIONAL_PHRASES):
            return Geography(name=name, level="metro")

    m = _NICKNAME_RE.search(text)
    if m:
        name = _CITY_NICKNAMES[m.group(0).lower()]
        if re.match(r"\s+(?i:county)\b", text[m.end():]):
            city, _, state = name.partition(", ")
            return Geography(name=f"{city} County, {state}" if state else f"{city} County", level="county")
        return Geography(name=name, level="place")

    for m in _CITY_STATE_RE.finditer(text):
        state = _state_from(m.group(2))
        if not state or state == "district of columbia":
            continue  # "Washington, D.C." is the District, handled below
        words = m.group(1).split()
        while words and words[0].lower() in _LEADING_NOT_PLACE:
            words = words[1:]
        city = " ".join(words)
        low = city.lower()
        if not city or low in _STATE_FIPS or low in _NOT_A_PLACE or low in _NON_US:
            continue
        if any(_word_bounded(low, p) for p in _NATIONAL_PHRASES):
            continue
        if re.search(r"\b(county|parish|borough)$", low):
            return Geography(name=f"{city}, {state.title()}", level="county")
        return Geography(name=f"{city}, {state.title()}", level="place")
    return None


def resolve_geography(text: str) -> Optional[Geography]:
    """Return the geography a claim is about, or None if it names no place.

    A sub-state place returns level="place", which no structured series can
    satisfy — that is deliberate, and routes the claim to grounded search rather
    than to the containing state.
    """
    if not text:
        return None
    lowered = " ".join(text.lower().split())

    local = _local_place(text)
    if local is not None:
        return local

    if _DC_RE.search(lowered):
        return Geography(name="District Of Columbia", level="state",
                         fips=_STATE_FIPS["district of columbia"])

    county = _COUNTY_RE.search(text)
    if county:
        # Below the state level: no fixed series answers it, so the claim goes
        # to the dataset search, which can resolve it, or is declined.
        return Geography(name=f"{county.group(1)} {county.group(2)}", level="county")

    for word in _AMBIGUOUS_DEMONYMS:
        if re.search(rf"\b{word}s?\b", lowered):
            return Geography.unknown(word.title() + "s")
    for word in _DEMONYMS_BY_LENGTH:
        if re.search(rf"\b{word}s?\b", lowered):
            state = _DEMONYMS[word]
            return Geography(name=state.title(), level="state", fips=_STATE_FIPS[state])

    for state in _STATES_BY_LENGTH:
        m = _word_bounded(lowered, state)
        if not m:
            continue
        if looks_like_substate_place(lowered, state, m):
            # Capture the whole place name for the citation and for the
            # grounded-search query, e.g. "new york city".
            tail = lowered[m.end():m.end() + 24].strip().split()
            word = re.sub(r"(['\u2019]s)?[^\w]*$", "", tail[0]) if tail else ""
            place = f"{state} {word}".strip()
            return Geography(name=place.title(), level="place")
        return Geography(name=state.title(), level="state", fips=_STATE_FIPS[state])

    for abbrev, full in _STATE_ABBREV.items():
        if _word_bounded(lowered, abbrev):
            return Geography(name=full.title(), level="state", fips=_STATE_FIPS[full])

    # Before the national phrases, because "america" is one of them and would
    # otherwise swallow "South America" and "Latin America".
    for country in _NON_US_BY_LENGTH:
        if _word_bounded(lowered, country):
            name = country.upper() if len(country.replace(".", "")) <= 3 else country.title()
            return Geography.unknown(name)

    for scope in _SUPRANATIONAL:
        if _word_bounded(lowered, scope):
            return Geography.unknown(scope.title() if scope != "eu" else "the EU")

    national = any(_word_bounded(lowered, phrase) for phrase in _NATIONAL_PHRASES)

    # A named place that is neither a state nor a known country ("Chicago's",
    # "the population of Paris") is checked even when the claim also says
    # "American" somewhere — otherwise it would be answered with the national
    # series.
    for rx in (_POSSESSIVE_RE, _METRIC_OF_RE):
        for match in rx.finditer(text):
            candidate = " ".join(match.group(1).split())
            low = candidate.lower()
            if any(_word_bounded(low, p) for p in _NATIONAL_PHRASES):
                continue
            if _proper_noun_is_unresolved(candidate):
                return Geography.unknown(candidate)

    if national:
        return Geography.national()

    # Nothing resolved. If the claim still names a proper noun in a locative
    # position ("on Mars", "in Springfield"), that is a place we cannot check,
    # not an absent constraint — so decline rather than falling through to the
    # national series.
    for match in _LOCATIVE_RE.finditer(text):
        candidate = " ".join(match.group(1).split())
        if candidate.lower() in _NOT_A_PLACE:
            continue
        if any(word.lower() in _NOT_A_PLACE for word in candidate.split()):
            continue
        # "Q" out of "Q2", stray initials — too short to be a place name.
        if len(candidate) < 3:
            continue
        return Geography.unknown(candidate)

    return None
