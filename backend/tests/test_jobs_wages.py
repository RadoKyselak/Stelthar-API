"""Jobs added in a year, and wage growth for a named month, from BLS."""
from unittest.mock import patch

import pytest

from claims.parser import parse_claim
from claims.verdict import CONTRADICTED, MOSTLY_ACCURATE, SUPPORTED, assess
from evidence.sources import bls as bls_source


@pytest.mark.parametrize("claim,metric", [
    ("The economy added 2.7 million jobs in 2023", "jobs_added"),
    ("Employers added 2.5 million jobs in 2023", "jobs_added"),
    ("Payrolls grew by 2.7 million in 2023", "jobs_added"),
    ("Job growth was 2.7 million in 2023", "jobs_added"),
    ("Wages grew 4.1% in December 2023", "wage_growth"),
    ("Average hourly earnings rose 4.1% in December 2023", "wage_growth"),
    # Levels keep their own series.
    ("Average hourly earnings were $34.27 in December 2023", "avg_hourly_earnings"),
    ("Nonfarm payrolls were 157 million in December 2023", "nonfarm_employment"),
])
def test_routed(claim, metric):
    p = parse_claim(claim)
    assert p.metric is not None and p.metric.metric == metric, (claim, p.metric)


@pytest.mark.parametrize("claim", [
    "The economy added 303,000 jobs in March 2024",   # monthly: revised too much
    "Biden added 15 million jobs since 2021",         # several years
    "The economy added 15 million jobs under Biden",
    "The US added 200,000 jobs a month in 2023",      # a monthly pace
    "Job growth averaged 250,000 in 2023",
    "The economy lost 9 million jobs in 2020",        # a loss: sign not carried
    "Wages grew 4.1% in 2023",                        # Dec/Dec or annual average?
    "Real wages grew 0.8% in December 2023",
    "Median weekly earnings rose 5% in December 2023",
])
def test_not_matched(claim):
    p = parse_claim(claim)
    assert p.metric is None or p.metric.metric not in ("jobs_added", "wage_growth"), claim


def _rows(data):
    return [{"year": y, "period": p, "periodName": p, "value": v} for (y, p, v) in data]


def _fake(rows):
    async def _fetch_raw(series_id, start, end):
        return rows
    return _fetch_raw


@pytest.mark.asyncio
class TestJobsAdded:
    ROWS = _rows([("2022", "M12", "154342"), ("2023", "M11", "156600"), ("2023", "M12", "156857")])

    async def test_december_over_december(self):
        with patch.object(bls_source, "_fetch_raw", _fake(self.ROWS)):
            out = await bls_source.fetch_change("jobs_added", "Jobs added", "CES0000000001", 2023)
        dp = out[0]
        assert dp.quantity.magnitude == pytest.approx(2_515_000)
        assert dp.observed.basis == "December 2022 to December 2023, seasonally adjusted"
        ex = dp.extra["excerpt"]
        assert ex["rows"] == [{"label": "2022 Dec", "value": "154,342"},
                              {"label": "2023 Dec", "value": "156,857"},
                              {"label": "Change", "value": "2,515", "cited": True}]
        assert ex["note"] == "156,857 − 154,342 = 2,515 thousand jobs."
        assert "revises" in dp.extra["caveat"]

    async def test_year_without_december_is_not_answered(self):
        rows = _rows([("2022", "M12", "154342"), ("2023", "M11", "156600")])
        with patch.object(bls_source, "_fetch_raw", _fake(rows)):
            assert await bls_source.fetch_change("jobs_added", "Jobs added", "CES0000000001", 2023) == []

    @pytest.mark.parametrize("claim,verdict", [
        ("The economy added 2.5 million jobs in 2023", SUPPORTED),
        ("The economy added 2.7 million jobs in 2023", MOSTLY_ACCURATE),   # the first report
        ("The economy added 4 million jobs in 2023", CONTRADICTED),
        ("The economy added 1.5 million jobs in 2023", CONTRADICTED),
    ])
    async def test_revision_band(self, claim, verdict):
        with patch.object(bls_source, "_fetch_raw", _fake(self.ROWS)):
            out = await bls_source.fetch_change("jobs_added", "Jobs added", "CES0000000001", 2023)
        v = assess(parse_claim(claim), out)
        assert v.verdict == verdict, (claim, v.headline)
        if verdict == MOSTLY_ACCURATE:
            assert "revised" in v.headline


@pytest.mark.asyncio
async def test_wage_growth_shows_earnings_not_an_index():
    rows = _rows([("2022", "M12", "32.94"), ("2023", "M12", "34.29")])
    with patch.object(bls_source, "_fetch_raw", _fake(rows)):
        out = await bls_source.fetch_inflation("wage_growth", "AHE change", "CES0500000003", 2023, 12)
    dp = out[0]
    assert dp.display_value() == "4.1%"
    assert dp.observed.basis.endswith("seasonally adjusted") and "not" not in dp.observed.basis
    labels = [r["label"] for r in dp.extra["excerpt"]["rows"]]
    assert labels == ["Earnings ($/hr), Dec 2022", "Earnings ($/hr), Dec 2023", "Change (%)"]


def test_revision_band_is_only_on_revised_counts():
    from claims.metrics import all_metrics
    assert {m.metric for m in all_metrics() if m.revision_band} == {"jobs_added"}
