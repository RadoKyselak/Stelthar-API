"""The metric registry: natural language phrasing -> an exact series to fetch.

This is what removes the LLM from the critical path. The old pipeline asked a
model to guess (source, table, line code, year, geography) *before any evidence
existed*, against a target of 12 BEA tables and 7 BLS series — and returned
"Inconclusive" whenever any field was wrong. Most claims never reached a real
lookup at all.

Matching here is deterministic, ordered longest-phrase-first, and costs nothing.
A claim that matches goes straight to a free government API and comes back with
an exact number; a claim that does not match falls through to grounded search
rather than being forced into a table lookup that cannot answer it.

Adding a metric is a data change, not a code change — which is the point.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from evidence.units import INDEX, JOBS, PERCENT, PERSONS, USD


@dataclass(frozen=True)
class MetricSpec:
    """One canonical metric and how to fetch it."""
    metric: str                  # canonical id
    label: str                   # human label used in output
    source: str                  # BLS | BEA | CENSUS | TREASURY | USASPENDING
    family: str                  # unit family from evidence.units
    # Phrases that identify this metric in a claim. Matched case-insensitively
    # as whole phrases; order within the registry does not matter because
    # lookup sorts by length.
    phrases: Tuple[str, ...]
    # Source-specific addressing.
    series_id: Optional[str] = None          # BLS
    bls_kind: Optional[str] = None           # "rate" | "cpi_yoy"
    bea_table: Optional[str] = None          # BEA
    bea_line: Optional[str] = None
    census_variable: Optional[str] = None    # Census ACS
    treasury_metric: Optional[str] = None
    # Geography levels this metric can actually be resolved at. Claims naming a
    # finer level than this are routed to grounded search instead of being
    # answered with the wrong jurisdiction.
    geo_levels: Tuple[str, ...] = ("nation",)
    notes: str = ""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
# BLS series ids are national, seasonally adjusted where that is the headline
# figure people quote. CPI is unadjusted (CUUR...) because the year-over-year
# inflation rate in circulation is computed from the unadjusted index.

_SPECS: List[MetricSpec] = [
    # ---- Labour market (BLS) ---------------------------------------------
    MetricSpec(
        metric="unemployment_rate", label="Unemployment rate", source="BLS",
        family=PERCENT, series_id="LNS14000000", bls_kind="rate",
        phrases=("unemployment rate", "unemployment", "jobless rate",
                 "rate of unemployment", "out of work"),
        notes="Seasonally adjusted, 16 years and over.",
    ),
    MetricSpec(
        metric="labor_force_participation", label="Labor force participation rate",
        source="BLS", family=PERCENT, series_id="LNS11300000", bls_kind="rate",
        phrases=("labor force participation rate", "labour force participation rate",
                 "labor force participation", "workforce participation"),
    ),
    MetricSpec(
        metric="nonfarm_employment", label="Total nonfarm employment", source="BLS",
        family=JOBS, series_id="CES0000000001", bls_kind="rate",
        phrases=("total nonfarm employment", "nonfarm payrolls", "nonfarm employment",
                 "number of jobs", "total employment"),
        notes="Level in thousands as published; normalised to jobs.",
    ),
    MetricSpec(
        metric="avg_hourly_earnings", label="Average hourly earnings, private",
        source="BLS", family=USD, series_id="CES0500000003", bls_kind="rate",
        phrases=("average hourly earnings", "average hourly wage", "hourly earnings"),
    ),
    # ---- Prices (BLS) -----------------------------------------------------
    MetricSpec(
        metric="inflation_rate", label="CPI-U inflation rate (year over year)",
        source="BLS", family=PERCENT, series_id="CUUR0000SA0", bls_kind="cpi_yoy",
        phrases=("inflation rate", "inflation", "consumer price inflation",
                 "cpi inflation", "rate of inflation", "consumer prices"),
        notes="Computed from the CPI-U all-items index, not seasonally adjusted.",
    ),
    MetricSpec(
        metric="core_inflation_rate", label="Core CPI inflation rate (year over year)",
        source="BLS", family=PERCENT, series_id="CUUR0000SA0L1E", bls_kind="cpi_yoy",
        phrases=("core inflation rate", "core inflation", "core cpi",
                 "inflation excluding food and energy"),
    ),
    MetricSpec(
        metric="cpi_index", label="CPI-U index level, all items", source="BLS",
        family=INDEX, series_id="CUUR0000SA0", bls_kind="rate",
        phrases=("consumer price index", "cpi index", "cpi level"),
    ),
    MetricSpec(
        metric="producer_price_inflation", label="PPI final demand (year over year)",
        source="BLS", family=PERCENT, series_id="WPUFD4", bls_kind="cpi_yoy",
        phrases=("producer price inflation", "producer prices", "ppi", "wholesale inflation"),
    ),

    # ---- Fiscal (Treasury) ------------------------------------------------
    MetricSpec(
        metric="national_debt", label="Total public debt outstanding",
        source="TREASURY", family=USD, treasury_metric="debt",
        phrases=("national debt", "public debt", "federal debt", "total public debt",
                 "debt outstanding", "the debt", "gross national debt"),
        notes="Daily 'debt to the penny' series; a point-in-time figure.",
    ),

    # ---- Macro aggregates (BEA NIPA) -------------------------------------
    # LineCode values are the NIPA line numbers for these tables.
    MetricSpec(
        metric="gdp", label="Gross domestic product (current dollars)", source="BEA",
        family=USD, bea_table="T10105", bea_line="1",
        phrases=("gross domestic product", "gdp", "the economy's output",
                 "national output", "economic output"),
    ),
    MetricSpec(
        metric="real_gdp_growth", label="Real GDP, percent change", source="BEA",
        family=PERCENT, bea_table="T10101", bea_line="1",
        phrases=("real gdp growth", "gdp growth", "economic growth",
                 "growth rate of the economy", "how fast the economy grew"),
    ),
    MetricSpec(
        metric="personal_income", label="Personal income", source="BEA",
        family=USD, bea_table="T20100", bea_line="1",
        phrases=("personal income", "total personal income", "national personal income"),
    ),
    MetricSpec(
        metric="defense_spending", label="Federal national defense expenditures",
        source="BEA", family=USD, bea_table="T31600", bea_line="2",
        phrases=("defense spending", "defence spending", "military spending",
                 "spending on defense", "spending on the military", "defense budget"),
    ),
    MetricSpec(
        metric="education_spending", label="Federal education expenditures",
        source="BEA", family=USD, bea_table="T31600", bea_line="14",
        phrases=("education spending", "spending on education", "education budget",
                 "federal education funding"),
    ),

    # ---- Demographics (Census ACS) ---------------------------------------
    MetricSpec(
        metric="population", label="Total population", source="CENSUS",
        family=PERSONS, census_variable="B01003_001E",
        geo_levels=("nation", "state"),
        phrases=("population", "how many people live", "total population",
                 "number of residents", "number of people"),
    ),
    MetricSpec(
        metric="median_household_income", label="Median household income",
        source="CENSUS", family=USD, census_variable="B19013_001E",
        geo_levels=("nation", "state"),
        phrases=("median household income", "median income", "typical household income",
                 "household income"),
    ),
    MetricSpec(
        metric="poverty_rate", label="Poverty rate", source="CENSUS",
        family=PERCENT, census_variable="B17001_002E",
        geo_levels=("nation", "state"),
        phrases=("poverty rate", "rate of poverty", "people in poverty",
                 "living in poverty", "poverty level"),
    ),
    MetricSpec(
        metric="uninsured_rate", label="Uninsured rate", source="CENSUS",
        family=PERCENT, census_variable="B27001_005E",
        geo_levels=("nation", "state"),
        phrases=("uninsured rate", "without health insurance", "uninsured",
                 "lack health insurance", "health insurance coverage"),
    ),
    MetricSpec(
        metric="median_home_value", label="Median home value", source="CENSUS",
        family=USD, census_variable="B25077_001E",
        geo_levels=("nation", "state"),
        phrases=("median home value", "median house price", "home values",
                 "typical home value", "house prices"),
    ),
    MetricSpec(
        metric="bachelors_or_higher", label="Bachelor's degree or higher",
        source="CENSUS", family=PERCENT, census_variable="B15003_022E",
        geo_levels=("nation", "state"),
        phrases=("bachelor's degree or higher", "college graduates", "college educated",
                 "with a college degree", "bachelors degree"),
    ),
]

BY_METRIC: Dict[str, MetricSpec] = {s.metric: s for s in _SPECS}

# Phrase -> spec, searched longest-first so "core inflation" wins over
# "inflation" and "median household income" over "household income".
_PHRASE_INDEX: List[Tuple[str, MetricSpec]] = sorted(
    ((p.lower(), s) for s in _SPECS for p in s.phrases),
    key=lambda kv: -len(kv[0]),
)


def all_metrics() -> List[MetricSpec]:
    return list(_SPECS)


def supported_metric_names() -> List[str]:
    return sorted(BY_METRIC)


def match_metric(text: str) -> Optional[MetricSpec]:
    """Find the metric a claim is about, or None.

    Longest phrase wins, so a claim mentioning "core inflation" is not matched
    as plain "inflation". Returning None is a real answer — it routes the claim
    to grounded search instead of forcing it into a table that cannot answer it.
    """
    if not text:
        return None
    haystack = f" {' '.join(text.lower().split())} "
    for phrase, spec in _PHRASE_INDEX:
        # Whole-phrase match with boundaries, so "unemployment" does not fire
        # inside "underemployment".
        if f" {phrase} " in haystack or haystack.startswith(f" {phrase} ") \
                or f" {phrase}," in haystack or f" {phrase}." in haystack \
                or f" {phrase}'s " in haystack:
            return spec
    return None
