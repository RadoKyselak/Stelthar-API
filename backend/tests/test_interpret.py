"""The interpreter: a claim read into a query by one (fake) model call, then
answered by the engine. No test reaches Anthropic."""
from datetime import date
from types import SimpleNamespace

import pytest

import config
from claims.verdict import CONTRADICTED, MEDIUM, SUPPORTED, Verdict
from evidence import cache as cache_mod
from services import derived, interpret
from services.verify_v2 import verify


def fake_client(raw, calls):
    async def create(**kw):
        calls.append(kw)
        block = SimpleNamespace(type="tool_use", name="query", input=raw, id="t1")
        return SimpleNamespace(content=[block], stop_reason="tool_use",
                               usage=SimpleNamespace(input_tokens=900, output_tokens=60))
    return SimpleNamespace(messages=SimpleNamespace(create=create))


@pytest.fixture
def interpreter_on(monkeypatch, tmp_path):
    monkeypatch.setattr(cache_mod, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(config, "AI_INTERPRETER", True)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(config, "AI_SEARCH_PROVIDER", "claude")

    def use(raw):
        calls = []
        monkeypatch.setattr(interpret, "_client", lambda: fake_client(raw, calls))
        return calls
    return use


class TestValidate:
    def test_a_figure_the_claim_does_not_state_is_rejected(self):
        raw = {"measure": "population", "operation": "value", "place": "Nashville, TN",
               "figure": "700,000", "comparator": "equals"}
        assert interpret.validate(raw, "Nashville has about 720,000 people.") is None

    def test_unknown_measures_and_ops_are_rejected(self):
        base = {"operation": "value", "place": "United States", "figure": "5%", "comparator": "equals"}
        assert interpret.validate({**base, "measure": "vibes"}, "vibes are 5%") is None
        assert interpret.validate({**base, "measure": "none"}, "x 5%") is None
        assert interpret.validate({**base, "measure": "unemployment_rate", "operation": "guess"}, "5%") is None

    def test_a_good_query(self):
        q = interpret.validate({"measure": "fred:APU0000708111", "operation": "change_since",
                                "place": "United States", "figure": "40%", "comparator": "greater than",
                                "since_year": 2024, "since_month": 1},
                               "Egg prices are up more than 40% since January 2024.")
        assert q is not None and q.fields == {"since_year": 2024, "since_month": 1}


async def test_unread_claim_answered_through_the_engine(interpreter_on, monkeypatch):
    calls = interpreter_on({"measure": "fred:APU0000708111", "operation": "change_since",
                            "place": "United States", "figure": "40%", "comparator": "greater than",
                            "since_year": 2024, "since_month": 1})
    rows = [(date(2024, 1, 1), 2.52), (date(2026, 9, 1), 3.80)]

    async def fred_rows(s, start=None, end=None, vintage=None):
        return rows if s.sid == "APU0000708111" else []
    monkeypatch.setattr(derived, "_rows", fred_rows)
    claim = "A dozen eggs costs more than 40% more than it did in January 2024."
    r = await verify(claim)
    assert r.verdict == SUPPORTED and r.tier == "dataset" and r.model_calls == 1
    assert r.confidence == MEDIUM  # a model read it
    assert len(calls) == 1 and calls[0]["tool_choice"] == {"type": "tool", "name": "query"}
    # The same claim again: read from the cache, nothing spent.
    r2 = await verify(claim)
    assert r2.verdict == SUPPORTED and r2.model_calls == 0 and len(calls) == 1


async def test_misread_guard_reads_the_claim_again(interpreter_on, monkeypatch):
    from evidence.datapoint import ANNUAL, Datapoint, Geography, Period
    from evidence.units import PERSONS, Quantity
    from services import verify_v2 as v2

    async def national(parsed):
        return [Datapoint(quantity=Quantity(341_780_000, PERSONS), metric="population",
                          label="Total population", observed=Period(2025, ANNUAL),
                          geography=Geography.national(), source="CENSUS", source_url="x")]
    monkeypatch.setattr(v2, "fetch_structured", national)
    monkeypatch.setattr(v2, "has_structured_adapter", lambda spec: True)
    seen = []

    async def interp(text, client_ip=None):
        seen.append(text)
        return interpret.Query(measure="population", operation="value", place="Nashville, TN",
                               figure="720,000", model_calls=1)

    async def run(q, text, parsed):
        dp = Datapoint(quantity=Quantity(721_074, PERSONS), metric="population",
                       label="Total population", observed=Period(2025, ANNUAL),
                       geography=Geography("Nashville-Davidson, Tennessee", "place", "4752006"),
                       source="CENSUS", source_url="x")
        return Verdict(verdict=SUPPORTED, confidence="High", headline="ok", evidence=[dp])
    monkeypatch.setattr(interpret, "interpret", interp)
    monkeypatch.setattr(interpret, "run", run)
    r = await verify("The Music City has 720,000 residents.")
    assert seen and r.verdict == SUPPORTED and r.confidence == MEDIUM
    assert any("looked wrong" in n for n in r.notes)


async def test_off_when_grounding_is_off(interpreter_on):
    calls = interpreter_on({"measure": "population", "operation": "value", "place": "X",
                            "figure": "1", "comparator": "equals"})
    await verify("Gotham has 1 million residents.", allow_grounding=False)
    assert calls == []


def test_misread_needs_figures_far_apart():
    from evidence.datapoint import ANNUAL, Datapoint, Geography, Period
    from evidence.units import PERSONS, Quantity
    from claims.parser import parse_claim
    p = parse_claim("The Music City has 720,000 residents.")
    dp = lambda v: Datapoint(quantity=Quantity(v, PERSONS), metric="population", label="x",
                             observed=Period(2025, ANNUAL), geography=Geography.national(),
                             source="CENSUS", source_url="x")
    assert interpret.misread_suspected(p, Verdict(CONTRADICTED, "High", "", evidence=[dp(341e6)]))
    assert not interpret.misread_suspected(p, Verdict(CONTRADICTED, "High", "", evidence=[dp(900e3)]))


def test_record_without_a_window_is_checked_as_a_value():
    q = interpret.validate({"measure": "unemployment_rate", "operation": "record", "place": "United States",
                            "figure": "14.8%", "comparator": "equals", "record": "highest",
                            "year": 2020, "month": 4}, "Unemployment peaked at 14.8% in April 2020.")
    assert q is not None and q.operation == "value"
