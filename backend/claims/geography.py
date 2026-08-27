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


def resolve_geography(text: str) -> Optional[Geography]:
    """Return the geography a claim is about, or None if it names no place.

    A sub-state place returns level="place", which no structured series can
    satisfy — that is deliberate, and routes the claim to grounded search rather
    than to the containing state.
    """
    if not text:
        return None
    lowered = " ".join(text.lower().split())

    for state in _STATES_BY_LENGTH:
        m = _word_bounded(lowered, state)
        if not m:
            continue
        if looks_like_substate_place(lowered, state, m):
            # Capture the whole place name for the citation and for the
            # grounded-search query, e.g. "new york city".
            tail = lowered[m.end():m.end() + 24].strip().split()
            place = f"{state} {tail[0]}".strip() if tail else state
            return Geography(name=place.title(), level="place")
        return Geography(name=state.title(), level="state", fips=_STATE_FIPS[state])

    for abbrev, full in _STATE_ABBREV.items():
        if _word_bounded(lowered, abbrev):
            return Geography(name=full.title(), level="state", fips=_STATE_FIPS[full])

    # Before the national phrases, because "america" is one of them and would
    # otherwise swallow "South America" and "Latin America".
    for country in _NON_US_BY_LENGTH:
        if _word_bounded(lowered, country):
            return Geography.unknown(country.title())

    for phrase in _NATIONAL_PHRASES:
        if _word_bounded(lowered, phrase):
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
