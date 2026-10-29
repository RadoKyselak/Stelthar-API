"""Which series a claim is checked against: the wrong one gives a wrong verdict.

Each row was a real failure before this change: a rent claim checked against
all-items inflation, interest on the debt against the debt itself, a head
count against a rate.
"""
import pytest

from claims.parser import parse_claim


@pytest.mark.parametrize("claim,metric,blocked", [
    # The debt: only the total outstanding is the debt.
    ("Interest on the debt topped $650 billion in 2023", "national_debt", True),
    ("The debt held by the public was $26 trillion in 2023", "national_debt", True),
    ("The debt ceiling is $31.4 trillion", "national_debt", True),
    ("The national debt was $34 trillion at the end of 2023", "national_debt", False),
    ("The debt hit $33 trillion in September 2023", "national_debt", False),
    # Inflation: a category has its own series, or is declined.
    ("Rent inflation hit 8% in 2023", "cpi_rent", False),
    ("Food prices rose 5.8% in 2023", "cpi_food", False),
    ("Grocery prices rose 5% in 2023", "cpi_food_at_home", False),
    ("Gas prices rose 5% in 2023", "cpi_gasoline", False),
    ("Car insurance inflation hit 20% in December 2023", "cpi_car_insurance", False),
    ("Housing costs rose 6% in 2023", "cpi_shelter", False),
    ("Tuition inflation was 5% in 2023", "inflation_rate", True),
    ("Inflation for childcare was 6% in 2023", "inflation_rate", True),
    ("Wages grew 4.1% in 2023, beating inflation", "inflation_rate", True),
    ("Inflation was 4.1% in 2023", "inflation_rate", False),
    ("Annual inflation was 4.1% in 2023", "inflation_rate", False),
    ("Inflation in June 2022 hit 9.1%", "inflation_rate", False),
    ("Inflation hit a 40-year high of 9.1% in June 2022", "inflation_rate", False),
    ("Core inflation was 3.9% in 2023", "core_inflation_rate", False),
    # Same words, different units, different series.
    ("Gas prices averaged $3.52 in 2023", "gas_price", False),
    ("Electricity prices rose 3.3% in December 2023", "cpi_electricity", False),
    # Shares and counts.
    ("About 8% of Americans had no health insurance in 2023", "uninsured_rate", False),
    ("11.5% of Americans lived in poverty in 2023", "poverty_rate", False),
    ("Median rent in California was $2,000 in 2023", "median_gross_rent", False),
    ("Texas had 30.5 million residents in 2023", "population", False),
    ("About 39 million people live in California", "population", False),
])
def test_routed_to(claim, metric, blocked):
    p = parse_claim(claim)
    assert p.metric is not None and p.metric.metric == metric, (claim, p.metric)
    assert bool(p.structured_blocker) == blocked, (claim, p.structured_blocker)


@pytest.mark.parametrize("claim", [
    "26 million Americans were uninsured in 2023",        # a count, not the rate
    "37.9 million people lived in poverty in 2023",       # a count, not the rate
    "Personal income grew 5% in Q2 2024",                 # a growth rate, not dollars
    "Texas had 5 million Hispanic residents in 2023",     # a subgroup
    "Texas added 500,000 residents in 2023",              # a change, not the total
    "Texas has 4 million people without health insurance",
    "California has 39 million people and 10 million immigrants",
])
def test_not_matched_to_a_series_that_cannot_answer(claim):
    p = parse_claim(claim)
    assert p.metric is None or p.structured_blocker, (claim, p.metric)
    if p.metric is not None:
        assert p.metric.metric != "population"
