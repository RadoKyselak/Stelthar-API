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
    out = []
    for _, var in scored[:limit]:
        label = cat[var]["label"]
        title, detail = _title(label)
        out.append(Candidate(
            provider="census_profile", id=var, title=title,
            units=_units(var, label), frequency=f"annual, ACS {year} 1-year",
            detail=detail, meta={"label": label, "table": cat[var]["table"], "year": str(year)},
        ))
    return out


# ------------------------------------------------------------------ places

_SUFFIX = re.compile(r"\s+(city|town|village|borough|municipality|CDP|"
                     r"city and borough|consolidated government|metro government|"
                     r"unified government|urban county|metropolitan government)"
                     r"(\s*\(balance\))?.*$", re.I)


async def _places(year: int, level: str) -> List[List[str]]:
    """[[NAME, state, code], ...] for every place or county ACS 1-year covers."""
    async def load():
        params = {"get": "NAME", "for": f"{level}:*", "in": "state:*"}
        if CENSUS_API_KEY:
            params["key"] = CENSUS_API_KEY
        data = await _get_json(f"{_BASE}/{year}/acs/acs1", params)
        return data[1:] if isinstance(data, list) and len(data) > 1 else []
    return await cached("census_places", f"{year}:{level}", 30 * 24 * 3600, load) or []


def _norm(s: str) -> str:
    return re.sub(r"[^a-z ]", "", s.lower().replace("saint ", "st ").replace("st. ", "st ")).strip()


async def resolve_place(name: str, year: int, claim: str) -> Optional[Geography]:
    """"Chicago" -> Chicago city, Illinois. A county when the name says so.
    When several places share the name and the claim names no state, the
    most populous is not guessed: the claim is declined."""
    raw = re.sub(r"^(the\s+)?city of\s+", "", (name or "").strip(), flags=re.I)
    level = "county" if re.search(r"\b(county|parish|borough)\b", raw, re.I) else "place"
    rows = await _places(year, level)
    if not rows:
        logger.info("No %s list for ACS %s", level, year)
    target = _norm(re.sub(r"\b(county|parish)\b", "", raw, flags=re.I) if level == "county" else raw)
    if not target:
        return None
    matches = []
    for nm, st, code in rows:
        base, _, state = nm.partition(", ")
        short = _SUFFIX.sub("", base)
        short = re.sub(r"\s+(county|parish|borough)$", "", short, flags=re.I) if level == "county" else short
        if _norm(short) == target:
            matches.append((nm, st, code, state))
    if len(matches) > 1:
        named = [m for m in matches if m[3] and m[3].lower() in claim.lower()]
        matches = named if len(named) == 1 else matches
    if len(matches) != 1:
        return None
    nm, st, code, _ = matches[0]
    return Geography(name=nm, level=level, fips=f"{st}{code}")


def _geo_params(geo: Optional[Geography]) -> Dict[str, str]:
    if geo is None or geo.level == "nation":
        return {"for": "us:1"}
    if geo.level == "state":
        return {"for": f"state:{geo.fips}"}
    if geo.level in ("place", "county") and geo.fips and len(geo.fips) > 2:
        return {"for": f"{geo.level}:{geo.fips[2:]}", "in": f"state:{geo.fips[:2]}"}
    raise ValueError(f"unsupported geography {geo}")


def _geo_id(geo: Optional[Geography]) -> str:
    if geo is None or geo.level == "nation":
        return "010XX00US"
    if geo.level == "state":
        return f"040XX00US{geo.fips}"
    if geo.level == "county":
        return f"050XX00US{geo.fips}"
    return f"160XX00US{geo.fips}"


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


async def fetch(c: Candidate, year: int, geo: Optional[Geography]) -> Optional[Datapoint]:
    var = c.id
    params = {"get": f"NAME,{var},{_moe_var(var)}", **_geo_params(geo)}
    if CENSUS_API_KEY:
        params["key"] = CENSUS_API_KEY

    async def load():
        data = await _get_json(f"{_BASE}/{year}/{_DATASET}", params)
        return data if isinstance(data, list) and len(data) > 1 else None

    data = await cached("census_profile_value", f"{year}:{var}:{params.get('for')}:{params.get('in','')}",
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
    if geography.level in ("place", "county"):
        geography = Geography(name=name, level=geography.level, fips=geography.fips)
    table = c.meta.get("table", var[:4])
    extra: Dict[str, Any] = {"table": table, "dataset": _DATASET}
    notes = []
    if moe is not None:
        extra["margin_of_error"] = moe * scale
        unit = "%" if family == PERCENT else ""
        notes.append(f"Survey estimate, ±{moe:g}{unit} (90% margin of error).")
    extra["caveat"] = " ".join(notes) if notes else None
    if not extra["caveat"]:
        extra.pop("caveat")
    measure = c.title if not c.detail else f"{c.title} ({c.detail.split(';')[0]})"
    return Datapoint(
        quantity=Quantity(magnitude=value * scale, family=family, raw=str(raw)),
        metric=f"census:{var}",
        label=measure,
        observed=Period(year=year, kind=ANNUAL, basis="ACS 1-year estimate"),
        geography=geography,
        source="CENSUS",
        source_url=f"https://data.census.gov/table/ACSDP1Y{year}.{table}?g={_geo_id(geo)}",
        series_id=var,
        raw_value=str(raw),
        extra=extra,
    )
