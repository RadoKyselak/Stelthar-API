"""American Community Survey data profiles (DP02-DP05) as a searchable dataset.

About 1,000 published estimates: age, birthplace, language, education,
veterans, disability, income, poverty, health insurance, commuting, housing
tenure, rent, home value, broadband and more, for the nation, every state,
every county and every city of 65,000+ people, each with its margin of error.

The catalog (variable names and labels) is public metadata, cached for a week.
Values come from the Census API with the project's key.
"""
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx

from config import API_TIMEOUTS, CENSUS_API_KEY, logger
from evidence.cache import cached
from evidence.datapoint import ANNUAL, Datapoint, Geography, Period
from evidence.datasets import Candidate, query_tokens, tokens
from evidence.excerpt import moe_excerpt
from evidence.sources import SourceUnavailable
from evidence.units import PERCENT, PERSONS, USD, Quantity

_BASE = "https://api.census.gov/data"
_DATASET = "acs/acs1/profile"
_TABLES = {
    "DP02": "Selected Social Characteristics",
    "DP03": "Selected Economic Characteristics",
    "DP04": "Selected Housing Characteristics",
    "DP05": "Demographic and Housing Estimates",
}
_VAR_RE = re.compile(r"DP0[2-5]_\d{4}P?E")
_WEEK = 7 * 24 * 3600


def available() -> bool:
    return bool(CENSUS_API_KEY)


async def _get_json(url: str, params: Dict[str, str]) -> Any:
    try:
        async with httpx.AsyncClient(timeout=API_TIMEOUTS.CENSUS, follow_redirects=False) as c:
            r = await c.get(url, params=params)
    except httpx.HTTPError as e:
        raise SourceUnavailable("CENSUS", f"request failed: {e}") from e
    if r.status_code in (204, 404):
        return None
    if r.status_code in (301, 302, 303, 307, 308):
        raise SourceUnavailable("CENSUS", "API key missing or invalid")
    if r.status_code >= 400:
        logger.warning("Census profile query rejected (%s): %s", r.status_code, (r.text or "")[:200])
        return None
    try:
        return r.json()
    except ValueError:
        return None


async def catalog(year: int) -> Dict[str, Dict[str, str]]:
    """{variable: {"label": ..., "table": ...}} for one ACS vintage."""
    async def load():
        data = await _get_json(f"{_BASE}/{year}/{_DATASET}/variables.json", {})
        if not data:
            return {}
        out = {}
        for k, v in (data.get("variables") or {}).items():
            if _VAR_RE.fullmatch(k):
                out[k] = {"label": str(v.get("label") or ""), "table": k[:4]}
        return out
    return await cached("census_profile_catalog", str(year), _WEEK, load) or {}


def _title(label: str) -> Tuple[str, str]:
    """"Percent!!PLACE OF BIRTH!!Total population!!Foreign-born" ->
    ("Foreign-born", "percent of total population; place of birth")."""
    parts = [p.strip() for p in label.split("!!") if p.strip()]
    kind = parts[0].lower() if parts else ""
    rest = parts[1:]
    if not rest:
        return label, ""
    leaf = rest[-1]
    section = rest[0].capitalize() if rest[0].isupper() else rest[0]
    # "Median (dollars)" alone does not say what it measures; its section does.
    if re.fullmatch(r"(median|mean|total)(\s*\([^)]*\))?", leaf, re.I) and len(rest) > 1:
        leaf = f"{section}: {leaf[0].lower()}{leaf[1:]}"
    universe = rest[1] if len(rest) > 2 else ""
    detail = []
    if kind == "percent" and universe:
        detail.append(f"percent of {universe.lower()}")
    elif universe:
        detail.append(universe.lower())
    detail.append(section.lower())
    return leaf, "; ".join(detail)


def _units(var: str, label: str) -> str:
    l = label.lower()
    if var.endswith("PE"):
        return "percent"
    if "dollars" in l:
        return "dollars"
    if "(years)" in l:
        return "years"
    if "minutes" in l:
        return "minutes"
    if "rate" in l:
        return "rate"
    return "count"


async def candidates(claim: str, year: int, drop_words, want: str, limit: int = 30) -> List[Candidate]:
    """Profile variables whose labels share the most words with the claim.

    `want` is the claim's unit family ("percent", "usd", ...); variables in
    a matching unit are ranked first. Scoring is cheap and recall-oriented:
    the picker makes the final choice.
    """
    cat = await catalog(year)
    if not cat:
        return []
    q = query_tokens(claim, drop_words)
    if not q:
        return []
    # Rarer words carry more weight.
    df: Dict[str, int] = {}
    toks = {}
    for var, info in cat.items():
        t = tokens(info["label"].replace("!!", " "))
        toks[var] = t
        for w in t:
            df[w] = df.get(w, 0) + 1
    n = len(cat)
    scored = []
    for var, t in toks.items():
        hit = q & t
        if not hit:
            continue
        score = sum(1.0 + (n / (1 + df[w])) ** 0.5 / 10 for w in hit)
        # A word in the estimate's own name ("Total population", "Foreign-born")
        # says far more than one in its category path.
        leaf = tokens(_title(cat[var]["label"])[0])
        score += 2.0 * len(q & leaf)
        units = _units(var, cat[var]["label"])
        if want == PERCENT and units == "percent":
            score += 1.5
        elif want == USD and units == "dollars":
            score += 1.5
        elif want in (PERSONS,) and units == "count":
            score += 1.0
        elif want == PERCENT and units != "percent":
            score -= 1.0
        scored.append((score, var))
    scored.sort(reverse=True)
    # The same estimate appears in more than one table ("Total population"
    # under both age and race): offer it once, from the first table.
    # Totals live first in DP05 (demographics), so that copy is kept.
    def pref(v):
        return (0 if v.startswith("DP05") else 1, v)

    best = {}
    for score, var in scored:
        key = (_title(cat[var]["label"])[0].lower(), _units(var, cat[var]["label"]))
        if key not in best or pref(var) < pref(best[key][1]):
            best[key] = (max(score, best.get(key, (score, var))[0]), var)
    scored = sorted(best.values(), reverse=True)
    return [_candidate(var, cat[var], year) for _, var in scored[:limit]]


def _candidate(var: str, info: Dict[str, str], year: int) -> Candidate:
    label = info["label"]
    title, detail = _title(label)
    return Candidate(
        provider="census_profile", id=var, title=title,
        units=_units(var, label), frequency=f"annual, ACS {year} 1-year",
        detail=detail, meta={"label": label, "table": info["table"], "year": str(year)},
    )


# A fixed series that stops at states has a twin in the profile tables, which
# reach counties and cities. Found by the catalog's own label rather than the
# variable number, because profile numbers shift from year to year.
_TWINS = {
    "population": ("Estimate!!SEX AND AGE!!Total population", False),
    "median_household_income": ("!!Total households!!Median household income (dollars)", False),
    "median_home_value": ("!!VALUE!!Owner-occupied units!!Median (dollars)", False),
    "median_gross_rent": ("!!GROSS RENT!!Occupied units paying rent!!Median (dollars)", False),
    "poverty_rate": ("BELOW THE POVERTY LEVEL!!All people", True),
    "uninsured_rate": ("!!Civilian noninstitutionalized population!!No health insurance coverage", True),
    "bachelors_or_higher": ("!!Population 25 years and over!!Bachelor's degree or higher", True),
}


async def twin(metric: str, year: int) -> Optional[Candidate]:
    """The profile estimate that is the same measure as a fixed series, or None.
    Exactly one variable must carry the label; anything else is not a twin."""
    spec = _TWINS.get(metric)
    if spec is None:
        return None
    suffix, percent = spec
    cat = await catalog(year)
    hits = [v for v, info in cat.items()
            if info["label"].endswith(suffix) and v.endswith("PE") == percent]
    # The same estimate repeated in another table: keep DP05's copy, as above.
    hits.sort(key=lambda v: (0 if v.startswith("DP05") else 1, v))
    if not hits or (len(hits) > 1 and not hits[0].startswith("DP05")):
        return None
    return _candidate(hits[0], cat[hits[0]], year)


# ------------------------------------------------------------------ places
# Any place Census publishes a profile for: cities and towns, counties (and
# New York City's boroughs), metro and micro areas, and ZIP codes. The 1-year
# survey covers places of 65,000+; smaller ones come from the 5-year survey,
# which is said in the answer.

_SUFFIX = re.compile(r"\s+(city|town|village|borough|municipality|CDP|"
                     r"city and borough|consolidated government|metro government|"
                     r"unified government|urban county|metropolitan government)"
                     r"(\s*\(balance\))?.*$", re.I)
_LAST_SUFFIX = re.compile(r"\s+(city|town|village|borough|municipality|CDP)$", re.I)
_LEVEL_FOR = {"place": "place", "county": "county",
              "metro": "metropolitan statistical area/micropolitan statistical area",
              "zcta": "zip code tabulation area"}
_MONTH = 30 * 24 * 3600

# Names people use that Census does not: (level, Census short name, state).
_ALIASES = {
    "nyc": ("place", "new york", "new york"),
    "new york city": ("place", "new york", "new york"),
    "brooklyn": ("county", "kings", "new york"),
    "manhattan": ("county", "new york", "new york"),
    "bronx": ("county", "bronx", "new york"),
    "the bronx": ("county", "bronx", "new york"),
    "queens": ("county", "queens", "new york"),
    "staten island": ("county", "richmond", "new york"),
    "honolulu": ("place", "urban honolulu", "hawaii"),
    "nashville": ("place", "nashville", "tennessee"),
    "louisville": ("place", "louisville", "kentucky"),
}


async def _places(year: int, level: str, survey: str = "acs1", state: str = "") -> List[List[Any]]:
    """[[NAME, population, code], ...] for every place of one kind a survey
    covers. The code is state+place or state+county FIPS, or the CBSA code."""
    async def load():
        params = {"get": "NAME,B01003_001E", "for": f"{_LEVEL_FOR[level]}:*"}
        if level in ("place", "county"):
            params["in"] = f"state:{state}" if state else "state:*"
        if CENSUS_API_KEY:
            params["key"] = CENSUS_API_KEY
        data = await _get_json(f"{_BASE}/{year}/acs/{survey}", params)
        if not isinstance(data, list) or len(data) < 2:
            return []
        head, out = data[0], []
        for row in data[1:]:
            rec = dict(zip(head, row))
            code = (rec.get("state", "") + rec.get(level, "")) if level in ("place", "county") \
                else rec.get(_LEVEL_FOR[level], "")
            try:
                pop = int(float(rec.get("B01003_001E") or 0))
            except (TypeError, ValueError):
                pop = 0
            out.append([rec.get("NAME", ""), pop, code])
        return out
    return await cached("census_places2", f"{survey}:{year}:{level}:{state}", _MONTH, load) or []


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^a-z ]", "", s.lower().replace("saint ", "st ")
                           .replace("st. ", "st ").replace("ft. ", "fort ")).split())


def _names(base: str, level: str) -> set:
    """The ways a Census place name can be written: "Kansas City city" is
    "Kansas City"; "New York city" is "New York" and "New York City";
    "Nashville-Davidson metropolitan government (balance)" is also "Nashville";
    "San Buenaventura (Ventura) city" is also "Ventura"."""
    if level == "county":
        return {_norm(re.sub(r"\s+(county|parish|borough|census area|city and borough|municipality)$",
                             "", base, flags=re.I))}
    out = set()
    for alt in re.findall(r"\(([^)]+)\)", base):
        if alt.lower() != "balance":
            out.add(_norm(alt))
    plain = re.sub(r"\s*\([^)]*\)", "", base)
    short = _SUFFIX.sub("", plain)
    out.add(_norm(short))
    one = _LAST_SUFFIX.sub("", plain)
    out.add(_norm(one))
    if one != plain:
        out.add(_norm(one + " city"))
    if re.search(r"government|\(balance\)", base, re.I):
        out.add(_norm(re.split(r"[-/]", short)[0]))
    return out


def _metro_parts(nm: str) -> Tuple[List[str], set, bool]:
    """"Dallas-Fort Worth-Arlington, TX Metro Area" ->
    (["dallas", "fort worth", "arlington"], {"texas"}, True)."""
    from claims.geography import USPS
    base, _, rest = nm.partition(", ")
    parts = [_norm(x) for x in base.split("-")]
    abbrevs = (rest.split() or [""])[0].split("-")
    states = {USPS[a] for a in abbrevs if a in USPS}
    return parts, states, "metro area" in rest.lower() or "metropolitan" in rest.lower()


def _states_named(text: str) -> set:
    from claims.geography import USPS, _STATE_FIPS
    low = text.lower()
    out = {st for st in _STATE_FIPS if re.search(rf"(?<![a-z]){re.escape(st)}(?![a-z])", low)}
    out |= {USPS[a] for a in re.findall(r",\s*([A-Z]{2})\b", text) if a in USPS}
    return out


def _choose(matches: List[Tuple[str, int, str, set]], only_state: str, named: set):
    """One place out of several with the same name: the state the claim names,
    else the one at least three times as populous as the next (Portland,
    Oregon over Portland, Maine). Otherwise none: guessing would be wrong."""
    if only_state:
        matches = [m for m in matches if only_state in m[3]]
    elif len(matches) > 1 and named:
        hit = [m for m in matches if m[3] & named]
        if hit:
            matches = hit
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        ranked = sorted(matches, key=lambda m: -m[1])
        if ranked[0][1] >= 3 * max(ranked[1][1], 1):
            return ranked[0]
    return None


def _match(rows, level: str, target: str) -> List[Tuple[str, int, str, set]]:
    out = []
    for nm, pop, code in rows:
        if level == "metro":
            parts, states, _ = _metro_parts(nm)
            want = target.split("|")
            if all(w in parts for w in want):
                out.append((nm, pop, code, states))
            continue
        base, _, state = nm.partition(", ")
        if target in _names(base, level):
            out.append((nm, pop, code, {state.lower()}))
    if level == "metro" and len(out) > 1:
        # A metro named after the city first ("Atlanta-Sandy Springs-…") over
        # one that only lists it; metropolitan over micropolitan.
        lead = [m for m in out if _metro_parts(m[0])[0][0] == target.split("|")[0]]
        out = lead or out
        big = [m for m in out if _metro_parts(m[0])[2]]
        out = big or out
    return out


async def locate(name: str, year: int, claim: str, level: Optional[str] = None
                 ) -> Optional[Tuple[Geography, str]]:
    """(the place, the survey that covers it: "acs1" or "acs5"), or None.

    "Chicago" -> Chicago city, Illinois. "Austin, TX" -> Austin city, Texas.
    "Bay Area" (as a metro) -> San Francisco-Oakland-Fremont, CA Metro Area.
    "ZIP Code 10001" -> ZCTA 10001 (5-year survey only)."""
    from claims.geography import _STATE_FIPS, _state_from
    raw = re.sub(r"^(the\s+)?(city|town|village) of\s+", "", (name or "").strip(), flags=re.I)
    raw = re.sub(r"['’]s$", "", raw).strip(" .,")
    z = re.fullmatch(r"(?:ZIP(?: Code)?\s*)?(\d{5})", raw, flags=re.I)
    if level == "zcta" or z:
        if not z:
            return None
        return Geography(name=f"ZIP Code {z.group(1)}", level="zcta", fips=z.group(1)), "acs5"

    only_state = ""
    if ", " in raw:
        head, _, tail = raw.rpartition(", ")
        st = _state_from(tail)
        if st:
            raw, only_state = head, st
    if level not in ("place", "county", "metro"):
        level = "county" if re.search(r"\b(county|parish|borough)\b", raw, re.I) else "place"
    target = _norm(re.sub(r"\b(county|parish)\b", "", raw, flags=re.I) if level == "county" else raw)
    if level == "metro":
        # "Dallas-Fort Worth" names two of the metro's cities.
        target = "|".join(n for n in (_norm(x) for x in re.split(r"[-\u2013/]", raw)) if n)
    alias = _ALIASES.get(target) if level != "metro" else None
    if alias and (not only_state or only_state == alias[2]):
        level, target, only_state = alias
    if not target:
        return None
    rest = claim.replace(name or "", " ")
    named = _states_named(rest)
    st_fips = _STATE_FIPS.get(only_state or (next(iter(named)) if len(named) == 1 else ""), "")

    tries = [(level, "acs1", "")]
    if level == "place":
        tries.append(("county", "acs1", ""))   # "Westchester" is a county
    if level == "metro":
        tries.append(("metro", "acs5", ""))    # small micro areas
    elif st_fips:
        tries += [(level, "acs5", st_fips)]    # towns under 65,000
        if level == "place":
            tries.append(("county", "acs5", st_fips))
    elif level == "county":
        tries.append(("county", "acs5", ""))

    for lvl, survey, st in tries:
        rows = await _places(year, lvl, survey, st)
        if not rows:
            logger.info("No %s list for %s %s %s", lvl, survey, year, st)
            continue
        found = _match(rows, lvl, target)
        if not found:
            continue
        pick = _choose(found, only_state, named)
        if pick is None:
            return None  # several places by that name, and nothing to choose by
        nm, _, code, _ = pick
        return Geography(name=nm, level=lvl, fips=code), survey
    return None


async def resolve_place(name: str, year: int, claim: str, level: Optional[str] = None) -> Optional[Geography]:
    got = await locate(name, year, claim, level)
    return got[0] if got else None


def _geo_params(geo: Optional[Geography]) -> Dict[str, str]:
    if geo is None or geo.level == "nation":
        return {"for": "us:1"}
    if geo.level == "state":
        return {"for": f"state:{geo.fips}"}
    if geo.level in ("place", "county") and geo.fips and len(geo.fips) > 2:
        return {"for": f"{geo.level}:{geo.fips[2:]}", "in": f"state:{geo.fips[:2]}"}
    if geo.level in ("metro", "zcta") and geo.fips:
        return {"for": f"{_LEVEL_FOR[geo.level]}:{geo.fips}"}
    raise ValueError(f"unsupported geography {geo}")


def _geo_id(geo: Optional[Geography]) -> str:
    if geo is None or geo.level == "nation":
        return "010XX00US"
    prefix = {"state": "040", "county": "050", "metro": "310", "zcta": "860"}.get(geo.level, "160")
    return f"{prefix}XX00US{geo.fips}"


def _moe_var(var: str) -> str:
    return var[:-2] + "PM" if var.endswith("PE") else var[:-1] + "M"


def _family(units: str, label: str):
    if units == "percent":
        return PERCENT, 1.0
    if units == "dollars":
        return USD, 1.0
    if units == "count" and "population" in label.lower():
        return PERSONS, 1.0
    return "unknown", 1.0


async def fetch(c: Candidate, year: int, geo: Optional[Geography],
                survey: str = "acs1") -> Optional[Datapoint]:
    var = c.id
    params = {"get": f"NAME,{var},{_moe_var(var)}", **_geo_params(geo)}
    if CENSUS_API_KEY:
        params["key"] = CENSUS_API_KEY

    async def load():
        data = await _get_json(f"{_BASE}/{year}/acs/{survey}/profile", params)
        return data if isinstance(data, list) and len(data) > 1 else None

    data = await cached("census_profile_value", f"{survey}:{year}:{var}:{params.get('for')}:{params.get('in','')}",
                        30 * 24 * 3600, load)
    if not data:
        return None
    row = dict(zip(data[0], data[1]))
    raw = row.get(var)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= -555555555:  # Census sentinel for "not available"
        return None
    moe = None
    try:
        moe = abs(float(row.get(_moe_var(var))))
        if moe > 1e8:
            moe = None
    except (TypeError, ValueError):
        pass

    label = c.meta.get("label", "")
    family, scale = _family(c.units, label)
    name = row.get("NAME") or (geo.name if geo else "United States")
    geography = geo if geo is not None else Geography.national()
    if geography.level in ("place", "county", "metro", "zcta"):
        geography = Geography(name=name, level=geography.level, fips=geography.fips)
    table = c.meta.get("table", var[:4])
    extra: Dict[str, Any] = {"table": table, "dataset": f"acs/{survey}/profile"}
    notes = []
    if survey == "acs5":
        notes.append(f"5-year estimate: the average of {year - 4} to {year}, the only survey "
                     f"Census publishes for places this size.")
    if moe is not None:
        extra["margin_of_error"] = moe * scale
        unit = "%" if family == PERCENT else ""
        notes.append(f"Survey estimate, ±{moe:g}{unit} (90% margin of error).")
    extra["caveat"] = " ".join(notes) if notes else None
    if not extra["caveat"]:
        extra.pop("caveat")
    extra["excerpt"] = moe_excerpt(raw, row.get(_moe_var(var)) if moe is not None else None,
                                   c.units)
    measure = c.title if not c.detail else f"{c.title} ({c.detail.split(';')[0]})"
    return Datapoint(
        quantity=Quantity(magnitude=value * scale, family=family, raw=str(raw)),
        metric=f"census:{var}",
        label=measure,
        observed=Period(year=year, kind=ANNUAL,
                        basis="ACS 1-year estimate" if survey == "acs1" else f"ACS 5-year estimate, {year - 4}\u2013{year}"),
        geography=geography,
        source="CENSUS",
        source_url=f"https://data.census.gov/table/ACSDP{1 if survey == 'acs1' else 5}Y{year}.{table}?g={_geo_id(geo)}",
        series_id=var,
        raw_value=str(raw),
        extra=extra,
    )
