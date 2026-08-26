"""Eval claims whose ground truth is derived from the authoritative source.

Two kinds of eval question get conflated, and they need different datasets:

  1. "Does the system correctly compare a claim against the official number?"
  2. "Does the system handle claims as they actually circulate?"

The existing claims.jsonl answers (2) using real PolitiFact rulings — the right
approach, and the module docstring there is right that inventing ground truth
would defeat the purpose. But it is 13 claims, seven of which its own authors
marked unanswerable, so it cannot measure (1) at all.

This file measures (1), and it does so without inventing anything. Ground truth
is read from the agency at build time and recorded with the series id and the
period, so each expected verdict is a mechanical consequence of a published
figure rather than a judgement call. A claim stating the published value is
Supported; the same claim with the value moved well outside the precision it was
written to is Contradicted. Neither label is an opinion.

What this deliberately does NOT claim to measure: whether the system is useful
on vague, framed, or politically contested claims. Those live in claims.jsonl,
and a good score here says nothing about them.

Regenerate with:  python -m eval.v2_dataset --refresh
"""
import argparse
import asyncio
import json
import pathlib
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

DATASET_PATH = pathlib.Path(__file__).parent / "v2_claims.jsonl"


@dataclass
class V2Claim:
    id: str
    claim: str
    expected_verdict: str          # Supported | Contradicted | No verdict
    # Populated only for No verdict: which reason the system must give.
    expected_reason: Optional[str] = None
    domain: str = ""
    # Provenance for the ground truth itself, so a label can be re-checked.
    truth_source: str = ""
    truth_series: str = ""
    truth_period: str = ""
    truth_value: Optional[float] = None
    notes: str = ""


def load_v2_claims(path: Optional[pathlib.Path] = None) -> List[V2Claim]:
    path = path or DATASET_PATH
    out: List[V2Claim] = []
    with open(path) as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                out.append(V2Claim(**json.loads(line)))
            except (json.JSONDecodeError, TypeError) as e:
                raise ValueError(f"{path}:{n}: {e}") from e
    return out


def _fmt_pct(v: float) -> str:
    return f"{v:.1f}".rstrip("0").rstrip(".")


async def build() -> List[V2Claim]:
    """Read live values from the agencies and derive claims from them."""
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    from evidence.sources import bls as bls_src
    from evidence.sources import treasury as tr_src
    from evidence.units import PERCENT

    claims: List[V2Claim] = []

    # ---- BLS unemployment, several years -------------------------------
    for year in (2019, 2021, 2022, 2023, 2024):
        dps = await bls_src.fetch_annual(
            "unemployment_rate", "Unemployment rate", "LNS14000000", PERCENT, year
        )
        if not dps:
            continue
        true_v = dps[0].quantity.magnitude
        shown = _fmt_pct(true_v)
        claims.append(V2Claim(
            id=f"bls-unemp-{year}-true",
            claim=f"The unemployment rate in {year} was {shown}%.",
            expected_verdict="Supported", domain="labor",
            truth_source="BLS", truth_series="LNS14000000",
            truth_period=f"{year} annual average", truth_value=true_v,
            notes="States the published annual average to one decimal place.",
        ))
        # Move it far enough that no reasonable tolerance covers it.
        wrong = round(true_v + 2.5, 1)
        claims.append(V2Claim(
            id=f"bls-unemp-{year}-false",
            claim=f"The unemployment rate in {year} was {_fmt_pct(wrong)}%.",
            expected_verdict="Contradicted", domain="labor",
            truth_source="BLS", truth_series="LNS14000000",
            truth_period=f"{year} annual average", truth_value=true_v,
            notes="Same claim shape, value moved 2.5 points off the published figure.",
        ))

    # ---- BLS inflation: annual and monthly bases ------------------------
    for year, month in ((2022, 6), (2022, None), (2021, None), (2023, None)):
        dps = await bls_src.fetch_inflation(
            "inflation_rate", "CPI-U inflation", "CUUR0000SA0", year, month=month
        )
        if not dps:
            continue
        true_v = dps[0].quantity.magnitude
        when = f"{['','January','February','March','April','May','June','July','August','September','October','November','December'][month]} {year}" if month else str(year)
        claims.append(V2Claim(
            id=f"bls-cpi-{year}{'-m%d' % month if month else ''}-true",
            claim=f"Inflation was {_fmt_pct(true_v)}% in {when}.",
            expected_verdict="Supported", domain="prices",
            truth_source="BLS", truth_series="CUUR0000SA0",
            truth_period=when, truth_value=true_v,
            notes="Monthly basis is month-over-same-month; annual is average-over-average.",
        ))

    # ---- Treasury debt, calendar year end -------------------------------
    for year in (2020, 2022, 2023):
        dps = await tr_src.fetch_national_debt(year=year)
        if not dps:
            continue
        true_v = dps[0].quantity.magnitude
        trillions = true_v / 1e12
        claims.append(V2Claim(
            id=f"treasury-debt-{year}-true",
            claim=f"The national debt was ${trillions:.1f} trillion in {year}.",
            expected_verdict="Supported", domain="fiscal",
            truth_source="TREASURY", truth_series="debt_to_penny",
            truth_period=f"{year} year end", truth_value=true_v,
            notes="Calendar year end, not fiscal — the two differ by ~$0.8T.",
        ))
        claims.append(V2Claim(
            id=f"treasury-debt-{year}-over",
            claim=f"The national debt was over ${trillions - 5:.0f} trillion in {year}.",
            expected_verdict="Supported", domain="fiscal",
            truth_source="TREASURY", truth_series="debt_to_penny",
            truth_period=f"{year} year end", truth_value=true_v,
            notes="Directional claim: the stated number is a threshold, not a target.",
        ))
        claims.append(V2Claim(
            id=f"treasury-debt-{year}-false",
            claim=f"The national debt was ${trillions + 12:.0f} trillion in {year}.",
            expected_verdict="Contradicted", domain="fiscal",
            truth_source="TREASURY", truth_series="debt_to_penny",
            truth_period=f"{year} year end", truth_value=true_v,
            notes="Value moved $12T off the published figure.",
        ))

    # ---- Honesty cases: the system must decline, and say why ------------
    honesty = [
        V2Claim(
            id="honest-future-year",
            claim="The unemployment rate in 2031 was 3.5%.",
            expected_verdict="No verdict",
            expected_reason="data_not_published_for_that_period",
            domain="honesty",
            notes="Series exists, period does not. Must not be reported as no series.",
        ),
        V2Claim(
            id="honest-substate-geography",
            claim="The population of New York City is 8.3 million.",
            expected_verdict="No verdict",
            expected_reason="no_official_series_at_that_geography",
            domain="honesty",
            notes="Must NOT be answered with New York State's 19.5M.",
        ),
        V2Claim(
            id="honest-no-series-crime",
            claim="Crime is out of control in our cities.",
            expected_verdict="No verdict",
            expected_reason="no_official_series_for_this_claim",
            domain="honesty",
            notes="Vague and non-quantitative; no wired-up series can address it.",
        ),
        V2Claim(
            id="honest-no-series-immigration",
            claim="They let in 10 million illegal immigrants.",
            expected_verdict="No verdict",
            expected_reason="no_official_series_for_this_claim",
            domain="honesty",
            notes="Genuine coverage gap. Guessing here is the failure mode.",
        ),
    ]
    claims.extend(honesty)
    return claims


async def refresh() -> None:
    claims = await build()
    with open(DATASET_PATH, "w") as f:
        f.write("# Generated by eval/v2_dataset.py --refresh.\n")
        f.write("# Ground truth is READ FROM THE AGENCY, not authored by hand.\n")
        for c in claims:
            f.write(json.dumps(asdict(c)) + "\n")
    print(f"wrote {len(claims)} claims to {DATASET_PATH}")
    by_v: Dict[str, int] = {}
    for c in claims:
        by_v[c.expected_verdict] = by_v.get(c.expected_verdict, 0) + 1
    for k, v in sorted(by_v.items()):
        print(f"  {k:14s} {v}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    if args.refresh:
        asyncio.run(refresh())
    else:
        for c in load_v2_claims():
            print(f"{c.id:34s} {c.expected_verdict:14s} {c.claim}")
