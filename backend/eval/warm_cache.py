"""Seed the response cache from payloads captured in tests/fixtures/.

Keyless BLS allows 25 requests a day, and building the eval set plus running it
exceeds that. Rather than measure whatever the quota happened to allow — which
is what produces a number that silently means "the weather that afternoon" —
this seeds the cache with the SAME payloads the live API returned, captured in
tests/fixtures/ and committed alongside the code.

This is not a substitute for a live run and does not pretend to be. It makes a
run reproducible: the same inputs on every invocation, so a change in the score
means a change in the code. The report says the cache was warmed, so nobody can
mistake it for a fresh measurement.

The honest fix for the underlying constraint is a free BLS registration key,
which raises the limit from 25/day to 500/day.

    python -m eval.warm_cache
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from evidence import cache  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def _series_rows(payload):
    series = payload.get("Results", {}).get("series", [])
    return series[0].get("data", []) if series else []


# _fetch_raw caches the ROW LIST it returns, not the HTTP envelope. Writing the
# envelope here produced a cache whose entries the reader could not parse, and
# the resulting AttributeError was swallowed by a broad handler upstream and
# surfaced as "no figure is published" — the same outage-as-evidence confusion
# this eval exists to catch.


def warm_bls_unemployment() -> int:
    payload = json.loads((FIXTURES / "bls_unemployment_2019_2024.json").read_text())
    rows = _series_rows(payload)
    n = 0
    # The adapter requests one year at a time, so partition the captured
    # multi-year payload into the per-year responses it would have received.
    for year in range(2019, 2025):
        year_rows = [r for r in rows if r.get("year") == str(year)]
        if not year_rows:
            continue
        cache.write("bls", f"LNS14000000:{year}:{year}", year_rows)
        n += 1
    return n


def warm_bls_cpi() -> int:
    payload = json.loads((FIXTURES / "bls_cpi_2021_2022.json").read_text())
    # fetch_inflation(2022) requests the pair (2021, 2022) in one call, which is
    # exactly the range this fixture was captured over.
    cache.write("bls", "CUUR0000SA0:2021:2022", _series_rows(payload))
    return 1


def main() -> int:
    total = warm_bls_unemployment() + warm_bls_cpi()
    print(f"warmed {total} cache entries from tests/fixtures/")
    for ns, st in cache.stats().items():
        print(f"  {ns:<10} {st['entries']:>3} entries  {st['bytes']:>8,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
