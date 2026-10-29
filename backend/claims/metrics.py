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
    # Text the row's LineDescription must contain. A renumbered table then
    # fails closed instead of returning a different aggregate.
    bea_expect: Optional[str] = None
    census_variable: Optional[str] = None    # Census ACS
    # "acs/acs1" for detailed tables (B…), "acs/acs1/subject" for the
    # published percentages (S…).
    census_dataset: str = "acs/acs1"
    treasury_metric: Optional[str] = None
    # Geography levels this metric can actually be resolved at. Claims naming a
    # finer level than this are routed to grounded search instead of being
    # answered with the wrong jurisdiction.
    geo_levels: Tuple[str, ...] = ("nation",)
    notes: str = ""
    # False when the metric is recognised but no typed adapter can fetch it
    # yet; such claims go straight to grounded search.
    structured: bool = True
    # Earliest year the configured series means what the label says. ACS
    # subject-table columns were renumbered in 2015 (S2701 C05 was "percent
    # insured", S1501 C02 was male-only), so earlier years are not answered
    # from them.
    min_year: Optional[int] = None
    # Census publishes a second official figure for some metrics (CPS ASEC
    # vs ACS). A claim quoting the other one is not wrong, so a mismatch
    # against ACS alone must not be reported as Contradicted.
    alternate_measure: str = ""
    # A variant of another metric selected by a qualifier in the claim, e.g.
    # the Black unemployment rate. `qualifier` is a regex; the parser switches
    # from `parent` to this spec when it matches, and the qualifier is then the
    # metric's own universe rather than an unsupported subgroup.
    parent: Optional[str] = None
    qualifier: Optional[str] = None
    # BLS LAUS series for a state, formatted with the 2-digit FIPS code:
    # annual figures use the not-seasonally-adjusted series (the basis of the
    # official annual average), monthly ones the seasonally adjusted series.
    state_series_annual: Optional[str] = None
    state_series_monthly: Optional[str] = None
    # Federal budget totals are reported by fiscal year; a claim saying "in
    # 2024" means FY2024. The datapoint says so explicitly.
    fiscal_year_default: bool = False
    # Floor for the equality tolerance as a share of the official value.
    # Survey prices are quoted loosely ("$3.50 a gallon"), and different
    # surveys (BLS, EIA, AAA) differ by a few percent.
    min_relative_tolerance: float = 0.0
    # How far apart two official measures can plausibly be. A miss larger
    # than this is Contradicted even when an alternate measure exists.
    alternate_band: float = 0.10
    # For counts the agency revises after the first report (payroll jobs):
    # a miss within this share of the current figure is Mostly accurate,
    # never Contradicted, because the claim may quote the first report.
    revision_band: float = 0.0


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
        geo_levels=("nation", "state"),
        state_series_annual="LAUST{fips}0000000000003",
        state_series_monthly="LASST{fips}0000000000003",
        notes="Seasonally adjusted, 16 years and over. States: BLS LAUS.",
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
        # Matched by wording in the parser ("added 2.7 million jobs in 2023"),
        # never by a bare phrase, and only for one stated calendar year.
        metric="jobs_added", label="Jobs added (change in nonfarm payroll employment)",
        source="BLS", family=JOBS, series_id="CES0000000001", bls_kind="change",
        phrases=(),
        # 2023: first reported as 2.7 million, 2.5 million after revisions.
        revision_band=0.15,
        notes="December to December, seasonally adjusted, as BLS states annual job growth.",
    ),
    MetricSpec(
        # The 12-month change in average hourly earnings, which BLS headlines
        # each month. Only for a named month: "wages grew 4% in 2023" could
        # mean December over December or one annual average over another.
        metric="wage_growth", label="Average hourly earnings, 12-month change",
        source="BLS", family=PERCENT, series_id="CES0500000003", bls_kind="cpi_yoy",
        phrases=(),
        notes="All private employees, seasonally adjusted.",
    ),
    MetricSpec(
        metric="avg_hourly_earnings", label="Average hourly earnings, private",
        source="BLS", family=USD, series_id="CES0500000003", bls_kind="rate",
        phrases=("average hourly earnings", "average hourly wage", "hourly earnings"),
    ),
    # ---- Unemployment by group (BLS CPS) ---------------------------------
    # Selected by qualifier ("the unemployment rate for Black workers") or by
    # phrase ("Black unemployment"). National only.
    *[
        MetricSpec(
            metric=f"unemployment_rate_{key}", label=f"Unemployment rate, {label}",
            source="BLS", family=PERCENT, series_id=series, bls_kind="rate",
            parent="unemployment_rate", qualifier=qualifier, phrases=phrases,
            notes=note,
        )
        for key, label, series, qualifier, phrases, note in [
            ("black", "Black or African American", "LNS14000006",
             r"\b(?:black|african[- ]americans?)\b",
             ("black unemployment rate", "black unemployment",
              "african american unemployment rate", "african american unemployment"),
             "Seasonally adjusted, 16 years and over."),
            ("white", "White", "LNS14000003",
             r"\bwhites?\b(?!\s+house)",
             ("white unemployment rate", "white unemployment"),
             "Seasonally adjusted, 16 years and over."),
            ("hispanic", "Hispanic or Latino", "LNS14000009",
             r"\b(?:hispanics?|latin[oax]s?|latine)\b",
             ("hispanic unemployment rate", "hispanic unemployment",
              "latino unemployment rate", "latino unemployment"),
             "Seasonally adjusted, 16 years and over."),
            ("asian", "Asian", "LNS14032183",
             r"\basians?\b",
             ("asian unemployment rate", "asian unemployment",
              "asian american unemployment"),
             "Seasonally adjusted, 16 years and over."),
            ("women", "women, 16 years and over", "LNS14000002",
             r"\b(?:women|female)\b",
             ("female unemployment rate", "women's unemployment rate",
              "unemployment rate for women", "unemployment among women"),
             "Seasonally adjusted."),
            ("men", "men, 16 years and over", "LNS14000001",
             r"\b(?:men|male)\b",
             ("male unemployment rate", "men's unemployment rate",
              "unemployment rate for men", "unemployment among men"),
             "Seasonally adjusted."),
            ("teens", "16 to 19 years", "LNS14000012",
             r"\b(?:teens?|teenagers?|teenage|(?:aged?\s+|ages\s+)?16\s*(?:to|-|through)\s*19)\b",
             ("teen unemployment rate", "teen unemployment",
              "teenage unemployment", "teenage unemployment rate"),
             "Seasonally adjusted."),
            ("youth", "16 to 24 years", "LNS14024887",
             r"\b(?:youth|young (?:people|adults|workers|americans)|(?:aged?\s+|ages\s+)?16\s*(?:to|-|through)\s*24)\b",
             ("youth unemployment rate", "youth unemployment"),
             "Seasonally adjusted."),
            ("veterans", "veterans, 18 years and over", "LNU04049526",
             r"\bveterans?\b",
             ("veteran unemployment rate", "veteran unemployment",
              "veterans unemployment rate", "veterans unemployment"),
             "Not seasonally adjusted."),
        ]
    ],

    # ---- Average consumer prices (BLS Average Price Data, U.S. city average)
    *[
        MetricSpec(
            metric=metric, label=label, source="BLS", family=USD,
            series_id=series, bls_kind="price", phrases=phrases,
            min_relative_tolerance=0.02, alternate_measure=alt, alternate_band=0.05, notes=note,
        )
        for metric, label, series, phrases, alt, note in [
            ("gas_price", "Average price of regular unleaded gasoline (per gallon)",
             "APU000074714",
             ("price of gas", "gas prices", "gas price", "gasoline prices",
              "gasoline price", "price of gasoline", "gallon of gas",
              "gallon of gasoline", "price at the pump", "prices at the pump",
              "gas was", "gas is", "gas cost", "gas costs", "gasoline was",
              "gasoline cost", "gas averaged", "gasoline averaged"),
             "EIA's weekly retail gasoline price and AAA's daily average, which "
             "differ from BLS's monthly average by a few cents",
             "BLS AP series APU000074714, U.S. city average."),
            ("egg_price", "Average price of a dozen grade A large eggs",
             "APU0000708111",
             ("price of eggs", "egg prices", "egg price", "dozen eggs",
              "carton of eggs", "cost of eggs", "eggs cost", "eggs costs",
              "eggs were", "eggs are", "eggs averaged", "eggs hit"),
             "", "BLS AP series APU0000708111, U.S. city average."),
            ("milk_price", "Average price of a gallon of whole milk",
             "APU0000709112",
             ("price of milk", "milk prices", "milk price", "gallon of milk",
              "milk cost", "milk costs", "milk was", "milk is", "milk averaged"),
             "", "BLS AP series APU0000709112, U.S. city average."),
            ("ground_beef_price", "Average price of 100% ground beef (per pound)",
             "APU0000703112",
             ("ground beef", "price of beef", "beef prices", "hamburger meat",
              "beef cost", "beef costs", "beef was", "beef is"),
             "", "BLS AP series APU0000703112, U.S. city average."),
            ("coffee_price", "Average price of ground roast coffee (per pound)",
             "APU0000717311",
             ("price of coffee", "coffee prices", "coffee price", "pound of coffee",
              "coffee cost", "coffee costs", "coffee was", "coffee is"),
             "", "BLS AP series APU0000717311, U.S. city average."),
            ("electricity_price", "Average price of electricity (per kWh)",
             "APU000072610",
             ("price of electricity", "electricity prices", "electricity price",
              "electricity rates", "cost of electricity", "per kilowatt-hour",
              "per kilowatt hour", "per kwh", "electricity costs", "electricity cost",
              "electricity was", "electricity is"),
             "EIA's average retail electricity price, which is measured differently",
             "BLS AP series APU000072610, U.S. city average."),
        ]
    ],

    # ---- Prices (BLS) -----------------------------------------------------
    MetricSpec(
        metric="inflation_rate", label="CPI-U inflation rate",
        source="BLS", family=PERCENT, series_id="CUUR0000SA0", bls_kind="cpi_yoy",
        phrases=("inflation rate", "inflation", "consumer price inflation",
                 "cpi inflation", "rate of inflation", "consumer prices"),
        notes="Computed from the CPI-U all-items index, not seasonally adjusted.",
    ),
    MetricSpec(
        metric="core_inflation_rate", label="Core CPI inflation rate",
        source="BLS", family=PERCENT, series_id="CUUR0000SA0L1E", bls_kind="cpi_yoy",
        phrases=("core inflation rate", "core inflation", "core cpi",
                 "inflation excluding food and energy"),
    ),
    # One category of consumer prices. Every series here was checked against
    # BLS's published 12-month change (Dec 2023 release) before being wired up.
    *[
        MetricSpec(
            metric=f"cpi_{key}", label=f"CPI-U inflation rate, {label}",
            source="BLS", family=PERCENT, series_id=series, bls_kind="cpi_yoy",
            phrases=tuple(
                [f"{w} inflation" for w in words]
                + [f"inflation for {w}" for w in words]
                + [f"{w} {n} {v}" for w in words for n in ("prices", "costs")
                   for v in ("rose", "increased", "climbed", "jumped", "fell", "dropped",
                             "declined", "were up", "were down", "went up", "went down",
                             "grew", "soared", "surged")]),
            notes="CPI-U, U.S. city average, not seasonally adjusted.",
        )
        for key, label, series, words in [
            ("food", "food", "CUUR0000SAF1", ("food",)),
            ("food_at_home", "food at home (groceries)", "CUUR0000SAF11",
             ("grocery", "groceries", "food at home", "supermarket")),
            ("energy", "energy", "CUUR0000SA0E", ("energy",)),
            ("gasoline", "gasoline (all types)", "CUUR0000SETB01",
             ("gas", "gasoline")),
            ("electricity", "electricity", "CUUR0000SEHF01", ("electricity", "electric")),
            ("shelter", "shelter", "CUUR0000SAH1", ("shelter", "housing")),
            ("rent", "rent of primary residence", "CUUR0000SEHA", ("rent", "rental")),
            ("medical", "medical care", "CUUR0000SAM",
             ("medical", "medical care", "health care", "healthcare")),
            ("new_vehicles", "new vehicles", "CUUR0000SETA01",
             ("new car", "new vehicle", "new-car", "new-vehicle")),
            ("used_vehicles", "used cars and trucks", "CUUR0000SETA02",
             ("used car", "used vehicle", "used-car", "used-vehicle")),
            ("car_insurance", "motor vehicle insurance", "CUUR0000SETE",
             ("car insurance", "auto insurance", "motor vehicle insurance")),
        ]
    ],
    MetricSpec(
        metric="cpi_index", label="CPI-U index level, all items", source="BLS",
        family=INDEX, series_id="CUUR0000SA0", bls_kind="rate",
        phrases=("consumer price index", "cpi index", "cpi level"),
    ),
    MetricSpec(
        metric="producer_price_inflation", label="PPI final demand inflation rate",
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

    # ---- Federal budget (Treasury Monthly Treasury Statement) -------------
    MetricSpec(
        metric="federal_deficit", label="Federal budget deficit",
        source="TREASURY", family=USD, treasury_metric="deficit",
        fiscal_year_default=True,
        phrases=("federal deficit", "budget deficit", "federal budget deficit",
                 "the deficit", "deficit", "annual deficit"),
        notes="MTS Table 1, fiscal-year-to-date total at September 30.",
    ),
    MetricSpec(
        metric="federal_receipts", label="Federal receipts (revenue)",
        source="TREASURY", family=USD, treasury_metric="receipts",
        fiscal_year_default=True,
        phrases=("federal revenue", "federal revenues", "federal receipts",
                 "government revenue", "tax revenue", "tax revenues",
                 "federal tax revenue", "the government collected"),
        notes="MTS Table 1, fiscal-year-to-date total at September 30.",
    ),
    MetricSpec(
        metric="federal_outlays", label="Federal outlays (spending)",
        source="TREASURY", family=USD, treasury_metric="outlays",
        fiscal_year_default=True,
        phrases=("federal spending", "federal outlays", "government spending",
                 "total federal spending", "the government spent",
                 "federal government spent", "federal government spending"),
        notes="MTS Table 1, fiscal-year-to-date total at September 30.",
    ),
    # Recognised so it is NOT read as the budget deficit; no adapter yet.
    MetricSpec(
        metric="trade_deficit", label="Trade deficit", source="BEA", family=USD,
        phrases=("trade deficit", "trade gap", "trade balance"),
        structured=False,
    ),

    # ---- Macro aggregates (BEA NIPA) -------------------------------------
    # LineCode values are the NIPA line numbers for these tables.
    MetricSpec(
        metric="gdp", label="Gross domestic product (current dollars)", source="BEA",
        family=USD, bea_table="T10105", bea_line="1", bea_expect="gross domestic product",
        phrases=("gross domestic product", "gdp", "the economy's output",
                 "national output", "economic output"),
    ),
    MetricSpec(
        metric="real_gdp_growth", label="Real GDP, percent change", source="BEA",
        family=PERCENT, bea_table="T10101", bea_line="1", bea_expect="gross domestic product",
        # BEA revises growth rates (advance, second, third estimate, annual
        # updates). A claim quoting an earlier estimate is not wrong; near
        # misses go to grounded search, which can find that estimate.
        alternate_measure="earlier BEA estimates of the same period, which BEA has since revised",
        alternate_band=0.5,
        phrases=("real gdp growth", "gdp growth", "economic growth",
                 "growth rate of the economy", "how fast the economy grew",
                 "gdp grew", "real gdp grew", "economy grew", "gdp rose",
                 "gdp increased", "gdp expanded", "economy expanded",
                 "gdp growth rate", "growth in gdp"),
        # Contraction verbs are deliberately absent: "GDP shrank 2.2%" asserts
        # -2.2%, and the value parser reads magnitudes unsigned.
    ),
    MetricSpec(
        metric="personal_income", label="Personal income", source="BEA",
        family=USD, bea_table="T20100", bea_line="1", bea_expect="personal income",
        phrases=("personal income", "total personal income", "national personal income"),
    ),
    MetricSpec(
        metric="defense_spending", label="Federal national defense expenditures",
        source="BEA", family=USD, bea_table="T31600", bea_line="2",
        phrases=("defense spending", "defence spending", "military spending",
                 "spending on defense", "spending on the military", "defense budget"),
        structured=False,
        notes="T31600 line mapping is unverified; routed to grounded search.",
    ),
    MetricSpec(
        metric="education_spending", label="Federal education expenditures",
        source="BEA", family=USD, bea_table="T31600", bea_line="14",
        phrases=("education spending", "spending on education", "education budget",
                 "federal education funding"),
        structured=False,
        notes="T31600 line mapping is unverified; routed to grounded search.",
    ),

    # ---- Demographics (Census ACS) ---------------------------------------
    MetricSpec(
        metric="population", label="Total population", source="CENSUS",
        family=PERSONS, census_variable="B01003_001E", min_year=2005,
        geo_levels=("nation", "state"),
        phrases=("population", "how many people live", "total population",
                 "number of residents", "number of people"),
    ),
    MetricSpec(
        metric="median_household_income", label="Median household income",
        source="CENSUS", family=USD, census_variable="B19013_001E", min_year=2005,
        alternate_measure=("the CPS ASEC estimate Census headlines in its annual "
                           "Income in the United States report"),
        geo_levels=("nation", "state"),
        phrases=("median household income", "median income", "typical household income",
                 "household income"),
    ),
    MetricSpec(
        metric="poverty_rate", label="Poverty rate", source="CENSUS",
        family=PERCENT, census_variable="S1701_C03_001E",
        census_dataset="acs/acs1/subject", min_year=2010,
        alternate_measure="the official poverty rate from CPS ASEC (and the Supplemental Poverty Measure)",
        # 2023: official CPS 11.1%, SPM 12.9%, ACS 12.5% — 11% apart.
        alternate_band=0.20,
        geo_levels=("nation", "state"),
        phrases=("poverty rate", "rate of poverty", "people in poverty",
                 "living in poverty", "lived in poverty", "live in poverty",
                 "lives in poverty", "poverty level"),
    ),
    MetricSpec(
        metric="uninsured_rate", label="Uninsured rate", source="CENSUS",
        family=PERCENT, census_variable="S2701_C05_001E",
        census_dataset="acs/acs1/subject", min_year=2015,
        alternate_measure="the CPS ASEC uninsured rate",
        alternate_band=0.15,
        geo_levels=("nation", "state"),
        phrases=("uninsured rate", "without health insurance", "uninsured",
                 "lack health insurance", "lacked health insurance", "no health insurance",
                 "health insurance coverage"),
    ),
    MetricSpec(
        metric="median_home_value", label="Median home value", source="CENSUS",
        family=USD, census_variable="B25077_001E", min_year=2005,
        geo_levels=("nation", "state"),
        phrases=("median home value", "median house price", "home values",
                 "typical home value", "house prices"),
    ),
    MetricSpec(
        metric="median_gross_rent", label="Median gross rent (monthly)", source="CENSUS",
        family=USD, census_variable="B25064_001E", min_year=2005,
        geo_levels=("nation", "state"),
        phrases=("median rent", "median gross rent", "median monthly rent", "typical rent"),
        notes="Gross rent: contract rent plus utilities and fuels the renter pays.",
    ),
    MetricSpec(
        metric="bachelors_or_higher", label="Bachelor's degree or higher, ages 25 and over",
        source="CENSUS", family=PERCENT, census_variable="S1501_C02_015E",
        census_dataset="acs/acs1/subject", min_year=2015,
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


# parent metric -> its qualifier-selected variants
VARIANTS: Dict[str, List[MetricSpec]] = {}
for _s in _SPECS:
    if _s.parent:
        VARIANTS.setdefault(_s.parent, []).append(_s)


def all_metrics() -> List[MetricSpec]:
    return list(_SPECS)


def supported_metric_names() -> List[str]:
    return sorted(BY_METRIC)


def match_metrics(text: str) -> List[MetricSpec]:
    """Every series a claim's wording matches, longest phrase first. The
    parser picks the first one in the claim's units: "gas prices rose 5%"
    and "gas prices were $3.50" share words but not a series."""
    if not text:
        return []
    haystack = f" {' '.join(text.lower().split())} "
    out: List[MetricSpec] = []
    for phrase, spec in _PHRASE_INDEX:
        if spec in out:
            continue
        if f" {phrase} " in haystack or f" {phrase}," in haystack \
                or f" {phrase}." in haystack or f" {phrase}'s " in haystack:
            out.append(spec)
    return out


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
