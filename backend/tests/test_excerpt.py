"""Source excerpts: the exact published rows behind each figure.

Every value in an excerpt must be the agency's own string (thousands
separators aside), exactly one row is the cited one, and the cited row's
value is the figure the verdict used.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from evidence import excerpt
from evidence.sources import bls as bls_source
from evidence.sources import treasury
from evidence.units import PERCENT, USD
from services import presentation


def _rows(series_id, data):
    return [{"year": y, "period": p, "periodName": p, "value": v} for (y, p, v) in data]


def _fake_fetch(table):
    async def _fetch_raw(series_id, start, end):
        return table
    return _fetch_raw


class TestPublished:
    @pytest.mark.parametrize("raw,shown", [
        ("4.0", "4.0"), ("34001493655565.48", "34,001,493,655,565.48"),
        ("-1832815989074.04", "-1,832,815,989,074.04"), ("27,720,709", "27,720,709"),
        ("296.311", "296.311"), ("(X)", "(X)"), ("", ""),
    ])
    def test_digits_are_never_changed(self, raw, shown):
        assert excerpt.published(raw) == shown

    def test_needs_exactly_one_cited_row(self):
        assert excerpt.make([excerpt.row("a", "1"), excerpt.row("b", "2")]) is None
        assert excerpt.make([excerpt.row("a", "1", True), excerpt.row("b", "2", True)]) is None
        assert excerpt.make([excerpt.row("a", "1", True)])["rows"][0]["cited"] is True


@pytest.mark.asyncio
class TestBLSExcerpts:
    async def test_published_annual_beside_december(self):
        data = _rows("LAUST480000000000003",
                     [("2023", f"M{m:02d}", "4.1") for m in range(1, 12)]
                     + [("2023", "M12", "3.7"), ("2023", "M13", "4.0")])
        with patch.object(bls_source, "_fetch_raw", _fake_fetch(data)):
            out = await bls_source.fetch_annual("unemployment_rate", "Unemployment rate",
                                                "LAUST480000000000003", PERCENT, 2023)
        ex = out[0].extra["excerpt"]
        assert ex["rows"] == [{"label": "2023 Dec", "value": "3.7"},
                              {"label": "2023 Annual", "value": "4.0", "cited": True}]
        assert ex["units"] == "percent" and "note" not in ex

    async def test_computed_annual_says_so(self):
        data = _rows("CEU0500000003", [("2023", f"M{m:02d}", "33.70") for m in range(1, 13)])
        with patch.object(bls_source, "_fetch_raw", _fake_fetch(data)):
            out = await bls_source.fetch_annual("hourly_earnings", "Average hourly earnings",
                                                "CEU0500000003", USD, 2023)
        ex = out[0].extra["excerpt"]
        cited = [r for r in ex["rows"] if r.get("cited")][0]
        # Two decimals, as BLS publishes earnings: not "33.7".
        assert cited == {"label": "2023 Annual average", "value": "33.70", "cited": True}
        assert "mean" in ex["note"]

    async def test_monthly_shows_neighbours(self):
        data = _rows("LNS14000000", [("2024", "M02", "3.9"), ("2024", "M03", "3.8"),
                                     ("2024", "M04", "3.9")])
        with patch.object(bls_source, "_fetch_raw", _fake_fetch(data)):
            out = await bls_source.fetch_monthly("unemployment_rate", "Unemployment rate",
                                                 "LNS14000000", PERCENT, 2024, 3)
        assert [r["label"] for r in out[0].extra["excerpt"]["rows"]] == ["2024 Feb", "2024 Mar", "2024 Apr"]
        assert out[0].extra["excerpt"]["rows"][1] == {"label": "2024 Mar", "value": "3.8", "cited": True}

    async def test_inflation_shows_the_arithmetic(self):
        data = _rows("CUUR0000SA0", [("2021", "M06", "271.696"), ("2022", "M06", "296.311")])
        with patch.object(bls_source, "_fetch_raw", _fake_fetch(data)):
            out = await bls_source.fetch_inflation("inflation", "CPI", "CUUR0000SA0", 2022, 6)
        ex = out[0].extra["excerpt"]
        assert [r["value"] for r in ex["rows"]] == ["271.696", "296.311", "9.1"]
        assert ex["rows"][2]["cited"] and out[0].display_value() == "9.1%"
        assert "(296.311 − 271.696) ÷ 271.696 × 100 = 9.06" in ex["note"]


class TestTreasuryExcerpts:
    def test_debt_record_digit_for_digit(self):
        dp = treasury._row_to_datapoint(
            {"record_date": "2023-12-29", "record_fiscal_year": "2024",
             "tot_pub_debt_out_amt": "34001493655565.48"}, "https://x", want_fiscal=False)
        assert dp.extra["excerpt"]["rows"] == [{
            "label": "12/29/2023", "value": "34,001,493,655,565.48", "cited": True}]
        assert dp.extra["excerpt"]["units"] == "Total Public Debt Outstanding, dollars"

    def test_statement_line_cites_the_asked_column(self):
        rows = [
            {"classification_desc": "FY 2024", "classification_id": "P", "record_date": "2024-09-30"},
            {"classification_desc": "Year-to-Date", "parent_id": "P", "record_date": "2024-09-30",
             "current_month_gross_rcpt_amt": "4918735644738.25",
             "current_month_gross_outly_amt": "6751551633812.29",
             "current_month_dfct_sur_amt": "1832815989074.04"},
        ]
        dp = treasury._mts_row_to_datapoint(rows, 2024, "deficit")
        ex = dp.extra["excerpt"]
        assert [r.get("cited", False) for r in ex["rows"]] == [False, False, True]
        assert ex["rows"][2] == {"label": "Deficit/Surplus (-)", "value": "1,832,815,989,074.04",
                                 "cited": True}
        assert ex["units"] == "FY 2024 Year-to-Date line, dollars"
        assert dp.quantity.magnitude == pytest.approx(1832815989074.04)


class TestPresentation:
    def test_malformed_excerpt_is_dropped(self):
        assert presentation._clean_excerpt({"rows": [{"label": "a", "value": "1"}]}) is None
        assert presentation._clean_excerpt("rows") is None
        ok = presentation._clean_excerpt({"heading": "H", "rows": [
            {"label": "a", "value": "1", "cited": True}]})
        assert ok == {"heading": "H", "units": "", "rows": [
            {"label": "a", "value": "1", "cited": True}], "note": ""}

    def test_web_source_keeps_the_quoted_sentence(self):
        s = presentation._web_source({"url": "https://www.bls.gov/x", "title": "T",
                                      "quote": "The rate  was\n4.0 percent."})
        assert s["quote"] == "The rate was 4.0 percent."


class TestClaudeQuotes:
    def test_cited_text_is_kept_once_per_page(self):
        from evidence import claude_search
        c1 = SimpleNamespace(url="https://bls.gov/a", title="A", cited_text="First words.")
        c2 = SimpleNamespace(url="https://bls.gov/a", title="A", cited_text="Later words.")
        content = [SimpleNamespace(type="text", text="x", citations=[c1]),
                   SimpleNamespace(type="text", text="y", citations=[c2])]
        cites, _, _ = claude_search._citations_and_segments(content)
        assert len(cites) == 1 and cites[0].quote == "First words."
