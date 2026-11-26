"""What "not", "every", "most", "at least", "between" and "and" do to a check
(claims/logic.py). Offline: the IRA roll call (Senate 325, Aug 7 2022:
R 50 Nay, D 48 Yea, I 2 Yea) and BLS's 3.6% unemployment rate for 2023."""
import pytest

from claims import logic
from claims.parser import parse_claim
from claims.verdict import CONTRADICTED, SUPPORTED, assess
from evidence.datapoint import Datapoint, Geography, Period
from evidence.sources import rollcall
from evidence.units import parse_quantity
from services import bills, votes

ABSTAIN = "abstain"

_MEMBERS = ([{"party": "R", "vote": rollcall.NAY, "last": f"R{i}", "first": "", "state": "XX"} for i in range(50)]
            + [{"party": "D", "vote": rollcall.YEA, "last": f"D{i}", "first": "", "state": "XX"} for i in range(48)]
            + [{"party": "I", "vote": rollcall.YEA, "last": f"I{i}", "first": "", "state": "XX"} for i in range(2)])
IRA = {"chamber": "Senate", "date": "2022-08-07", "members": _MEMBERS, "url": "https://www.senate.gov/x", "number": 325,
       "question": "On Passage of the Bill", "title": "t", "result": "Bill Passed", "yea": 50, "nay": 50,
       "tie_breaker": "Yea"}
IRA_ACT = "the Inflation Reduction Act"


@pytest.mark.parametrize("claim,want", [
    (f"Every Republican senator voted against {IRA_ACT}", SUPPORTED),
    (f"Not every Republican senator voted against {IRA_ACT}", CONTRADICTED),
    (f"No Democratic senator voted against {IRA_ACT}", SUPPORTED),
    (f"Most Republican senators voted against {IRA_ACT}", SUPPORTED),
    (f"Most Democratic senators voted against {IRA_ACT}", CONTRADICTED),
    (f"A majority of Republican senators voted for {IRA_ACT}", CONTRADICTED),
    (f"More than half of Democratic senators voted for {IRA_ACT}", SUPPORTED),
    (f"At least ten Republican senators voted for {IRA_ACT}", CONTRADICTED),
    (f"At least forty Democratic senators voted for {IRA_ACT}", SUPPORTED),
    (f"Fewer than five Republican senators voted for {IRA_ACT}", SUPPORTED),
    (f"Three Republican senators voted for {IRA_ACT}", CONTRADICTED),
    (f"Not all Democratic senators voted for {IRA_ACT}", CONTRADICTED),
    (f"Every Republican senator voted against {IRA_ACT} and every Democratic senator voted for it", SUPPORTED),
    (f"Every Republican senator voted against {IRA_ACT} and three Democratic senators voted against it", CONTRADICTED),
])
def test_vote_logic(claim, want):
    ref = bills._ref(claim)
    v = votes.judge_all(votes.read_all(claim, ref.span), [IRA], "the IRA")
    assert v is not None and v.verdict == want


_DP = Datapoint(quantity=parse_quantity("3.6%"), metric="unemployment_rate", label="Unemployment rate",
                observed=Period(2023), geography=Geography("United States"), source="BLS", source_url="https://bls.gov")


@pytest.mark.parametrize("claim,want", [
    ("The unemployment rate was 3.6% in 2023", SUPPORTED),
    ("The unemployment rate wasn't 3.6% in 2023", CONTRADICTED),
    ("The unemployment rate was not 5% in 2023", SUPPORTED),
    ("It is false that the unemployment rate was 3.6% in 2023", CONTRADICTED),
    ("It's not true that unemployment wasn't 3.6% in 2023", SUPPORTED),
    ("The unemployment rate was not above 5% in 2023", SUPPORTED),
    ("The unemployment rate was not below 3% in 2023", SUPPORTED),
    ("The unemployment rate was not above 3% in 2023", CONTRADICTED),
    ("The unemployment rate did not exceed 4% in 2023", SUPPORTED),
    ("The unemployment rate was between 3% and 4% in 2023", SUPPORTED),
    ("The unemployment rate was between 4% and 5% in 2023", CONTRADICTED),
    ("The unemployment rate was 3.6% in 2023, not 5%", SUPPORTED),
    ("The unemployment rate was 5% in 2023, not 3.6%", CONTRADICTED),
    ("The unemployment rate in 2023 was not only low, it was 3.6%", SUPPORTED),
    ("Unemployment, which had not been this low in years, was 3.6% in 2023", SUPPORTED),
    ("Not seasonally adjusted, the unemployment rate was 3.6% in 2023", SUPPORTED),
    ("The unemployment rate was never below 4% in 2023", ABSTAIN),
])
def test_number_logic(claim, want):
    p = parse_claim(claim)
    got = ABSTAIN if p.structured_blocker else assess(p, [_DP]).verdict
    assert got == want


def test_and_is_three_valued():
    assert logic.conjunction([SUPPORTED, None]) is None            # unknown part: unknown whole
    assert logic.conjunction([CONTRADICTED, None]) == CONTRADICTED  # a false part settles it
    assert logic.conjunction([SUPPORTED, "Mostly accurate"]) == "Mostly accurate"


def test_negating_a_threshold_is_the_other_threshold():
    assert parse_claim("The national debt is not under $30 trillion").comparator == "at least"
    assert parse_claim("Inflation was not more than 3% in 2023").comparator == "at most"
