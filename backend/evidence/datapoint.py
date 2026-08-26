"""A datapoint that carries where it actually came from.

The audit's dominant finding was that adapters reconstructed provenance from
the *request* instead of reading it from the *response*: USAspending stamped a
row with the fiscal year that was asked for, Treasury wrote a fiscal year for a
calendar-year question, Census wrote no period at all. That single pattern
caused every wrong-number bug, because the guard meant to catch a period
mismatch was comparing the claim's year against a field the adapter had just
copied from the claim.

The rule this module enforces:

    An adapter MUST populate `observed` and `geography` from the payload it
    received. The pipeline then calls `Datapoint.mismatch_against(request)` and
    refuses to score a datapoint whose period or geography does not answer what
    was asked, rather than relabelling it.

A datapoint that cannot state its own period is not evidence. `observed` is
therefore required, and `Period.unknown()` exists so an adapter can say so
explicitly — which the pipeline treats as a mismatch, not as a pass.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from evidence.units import Quantity, humanize

# Period kinds.
ANNUAL = "annual"
FISCAL_YEAR = "fiscal_year"
MONTHLY = "monthly"
QUARTERLY = "quarterly"
POINT_IN_TIME = "point_in_time"
UNKNOWN_PERIOD = "unknown"

_MONTH_NAMES = {
    1: "January", 2: "February", 3: "March", 4: "April", 5: "May", 6: "June",
    7: "July", 8: "August", 9: "September", 10: "October", 11: "November", 12: "December",
}


@dataclass(frozen=True)
class Period:
    """When a measurement refers to. Read from the payload, never assumed."""
    year: Optional[int]
    kind: str = ANNUAL
    month: Optional[int] = None
    quarter: Optional[int] = None
    # Free text describing exactly what the figure covers, e.g.
    # "annual average" or "as of 2023-09-30 (fiscal year end)".
    basis: str = ""
    # The exact date the record carries, when it has one.
    record_date: Optional[str] = None

    @staticmethod
    def unknown() -> "Period":
        return Period(year=None, kind=UNKNOWN_PERIOD, basis="period not reported by source")

    @property
    def is_known(self) -> bool:
        return self.kind != UNKNOWN_PERIOD and self.year is not None

    def covers_calendar_year(self, year: int) -> bool:
        """Does this period answer a question about calendar `year`?

        A fiscal year deliberately does NOT cover the calendar year of the same
        number: US FY2023 ended 2023-09-30, so it says nothing about October to
        December 2023. Treating the two as interchangeable is what let a true
        claim about the debt "in 2023" be contradicted by the FY-end figure.
        """
        if not self.is_known:
            return False
        if self.kind == FISCAL_YEAR:
            return False
        return self.year == year

    def covers_fiscal_year(self, year: int) -> bool:
        if not self.is_known:
            return False
        return self.kind == FISCAL_YEAR and self.year == year

    def describe(self) -> str:
        if not self.is_known:
            return "period unknown"
        if self.kind == FISCAL_YEAR:
            base = f"FY{self.year}"
        elif self.kind == MONTHLY and self.month:
            base = f"{_MONTH_NAMES.get(self.month, '')} {self.year}".strip()
        elif self.kind == QUARTERLY and self.quarter:
            base = f"Q{self.quarter} {self.year}"
        elif self.kind == POINT_IN_TIME and self.record_date:
            base = f"as of {self.record_date}"
        else:
            base = str(self.year)
        return f"{base} ({self.basis})" if self.basis else base


@dataclass(frozen=True)
class Geography:
    """Where a measurement refers to. Read from the payload, never assumed."""
    name: str                     # "Texas", "United States", "New York city, New York"
    level: str = "nation"         # nation | state | county | place | metro
    fips: Optional[str] = None

    @staticmethod
    def national() -> "Geography":
        return Geography(name="United States", level="nation")

    def matches(self, requested: "Geography") -> bool:
        if requested is None:
            return True
        if self.level != requested.level:
            return False
        if self.fips and requested.fips:
            return self.fips == requested.fips
        return self.name.strip().lower() == requested.name.strip().lower()


@dataclass(frozen=True)
class Request:
    """What the caller actually asked for, kept separate from what came back."""
    metric: str
    year: Optional[int] = None
    # "calendar" or "fiscal" — a claim saying "in FY2023" is a different
    # question from one saying "in 2023".
    year_basis: str = "calendar"
    geography: Optional[Geography] = None


@dataclass(frozen=True)
class Datapoint:
    """One authoritative measurement, with provenance read from its payload."""
    quantity: Quantity
    metric: str
    label: str
    observed: Period
    geography: Geography
    source: str                       # "BLS", "BEA", "CENSUS", "TREASURY", ...
    source_url: str                   # already redacted; see utils.urls
    series_id: Optional[str] = None
    # When the agency published/last-updated this figure.
    as_of: Optional[str] = None
    # The untouched value as the API rendered it, for auditability.
    raw_value: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def mismatch_against(self, request: Request) -> Optional[str]:
        """Return why this datapoint does NOT answer `request`, or None if it does.

        This is the guard the old pipeline lacked. It fails CLOSED: an unknown
        period is a mismatch, not an implicit pass, because the failure mode
        being prevented is exactly a datapoint that could not say when it was
        from being scored as though it matched.
        """
        if request.metric and self.metric != request.metric:
            return f"metric mismatch: asked for {request.metric}, got {self.metric}"

        if request.year is not None:
            if not self.observed.is_known:
                return (
                    f"source did not report a period, so it cannot be confirmed to "
                    f"cover {request.year}"
                )
            if request.year_basis == "fiscal":
                if not self.observed.covers_fiscal_year(request.year):
                    return (
                        f"asked for fiscal year {request.year}, evidence covers "
                        f"{self.observed.describe()}"
                    )
            elif not self.observed.covers_calendar_year(request.year):
                return (
                    f"asked for calendar year {request.year}, evidence covers "
                    f"{self.observed.describe()}"
                )

        if request.geography is not None and not self.geography.matches(request.geography):
            return (
                f"asked about {request.geography.name}, evidence is for "
                f"{self.geography.name}"
            )
        return None

    def answers(self, request: Request) -> bool:
        return self.mismatch_against(request) is None

    def display_value(self) -> str:
        return humanize(self.quantity.magnitude, self.quantity.family)

    def to_dict(self) -> Dict[str, Any]:
        """Serialise for the API response — provenance is part of the payload."""
        return {
            "metric": self.metric,
            "label": self.label,
            "value": self.quantity.magnitude,
            "value_display": self.display_value(),
            "unit": self.quantity.family,
            "raw_value": self.raw_value,
            "period": self.observed.describe(),
            "period_year": self.observed.year,
            "period_kind": self.observed.kind,
            "geography": self.geography.name,
            "geography_level": self.geography.level,
            "source": self.source,
            "source_url": self.source_url,
            "series_id": self.series_id,
            "as_of": self.as_of,
        }
