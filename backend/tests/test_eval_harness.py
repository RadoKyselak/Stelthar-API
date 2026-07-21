"""Tests for the eval harness itself (dataset loading + scoring math).

These validate the harness mechanics with hand-checkable synthetic inputs —
NOT a claim about real system accuracy. Actual accuracy numbers only come from
running `python -m eval.runner` against the live pipeline with real API keys.
"""
from eval.dataset import EvalClaim, load_claims
from eval.metrics import ClaimResult, compute_report, score_coverage_honesty, score_verdict


def _claim(**overrides) -> EvalClaim:
    defaults = dict(
        id="t1", claim="test claim", domain="test_domain",
        ground_truth_verdict="Supported", ground_truth_notes="",
        source_url="https://example.com", source_date="2024-01-01",
        expected_coverage="yes",
    )
    defaults.update(overrides)
    return EvalClaim(**defaults)


# ------------------------------------------------------------- real dataset

def test_seed_dataset_loads_and_is_nonempty():
    claims = load_claims()
    assert len(claims) >= 10
    assert all(c.source_url.startswith("https://") for c in claims)
    assert all(c.ground_truth_verdict in ("Supported", "Contradicted", "Mixed") for c in claims)


def test_seed_dataset_covers_uncovered_domains():
    """The whole point of including crime/immigration claims is to test
    coverage honesty — confirm the seed set actually has some."""
    claims = load_claims()
    uncovered = [c for c in claims if c.expected_coverage == "no"]
    assert len(uncovered) >= 3


def test_seed_dataset_covers_framing_traps():
    claims = load_claims()
    assert any(c.is_framing_trap for c in claims)


# --------------------------------------------------------- verdict scoring

def test_score_verdict_clean_match():
    claim = _claim(ground_truth_verdict="Supported")
    assert score_verdict(claim, "Supported") == "correct"
    assert score_verdict(claim, "Contradicted") == "wrong"


def test_score_verdict_mixed_inconclusive_is_correct():
    """Inconclusive on a Mixed (Half-True) claim is the RIGHT answer, not a miss."""
    claim = _claim(ground_truth_verdict="Mixed")
    assert score_verdict(claim, "Inconclusive") == "correct"


def test_score_verdict_mixed_confident_answer_is_overconfident():
    """Confidently picking a side on a nuanced claim is a distinct, worse
    failure mode than a plain wrong answer."""
    claim = _claim(ground_truth_verdict="Mixed")
    assert score_verdict(claim, "Supported") == "overconfident_on_mixed"
    assert score_verdict(claim, "Contradicted") == "overconfident_on_mixed"


def test_score_verdict_no_response():
    claim = _claim()
    assert score_verdict(claim, None) == "no_response"


# ------------------------------------------------------- coverage honesty

def test_coverage_honesty_not_applicable_when_covered():
    claim = _claim(expected_coverage="yes")
    result = ClaimResult(claim=claim, predicted_verdict="Supported",
                          predicted_confidence=0.9, had_datapoint=True, latency_seconds=1.0)
    assert score_coverage_honesty(claim, result) is None


def test_coverage_honesty_inconclusive_is_honest():
    claim = _claim(expected_coverage="no")
    result = ClaimResult(claim=claim, predicted_verdict="Inconclusive",
                          predicted_confidence=0.3, had_datapoint=False, latency_seconds=1.0)
    assert score_coverage_honesty(claim, result) == "honest"


def test_coverage_honesty_low_confidence_verdict_is_honest():
    """Even a Supported/Contradicted verdict counts as honest if confidence
    stayed low — the system flagged its own uncertainty."""
    claim = _claim(expected_coverage="no")
    result = ClaimResult(claim=claim, predicted_verdict="Supported",
                          predicted_confidence=0.2, had_datapoint=False, latency_seconds=1.0)
    assert score_coverage_honesty(claim, result) == "honest"


def test_coverage_honesty_confident_wrong_answer_is_overconfident():
    """The failure mode this metric exists to catch: guessing confidently on
    a claim with no real source behind it."""
    claim = _claim(expected_coverage="no")
    result = ClaimResult(claim=claim, predicted_verdict="Contradicted",
                          predicted_confidence=0.85, had_datapoint=False, latency_seconds=1.0)
    assert score_coverage_honesty(claim, result) == "overconfident"


# -------------------------------------------------------- full report math

def test_compute_report_basic_accuracy():
    claims = [_claim(id="a", ground_truth_verdict="Supported"),
              _claim(id="b", ground_truth_verdict="Contradicted")]
    results = [
        ClaimResult(claim=claims[0], predicted_verdict="Supported", predicted_confidence=0.9,
                    had_datapoint=True, latency_seconds=1.0),
        ClaimResult(claim=claims[1], predicted_verdict="Supported", predicted_confidence=0.9,
                    had_datapoint=True, latency_seconds=2.0),  # wrong
    ]
    report = compute_report(results)
    assert report.accuracy == 0.5
    assert report.latency_p50 in (1.0, 2.0)  # small-N percentile is one of the two


def test_compute_report_retrieval_recall_only_counts_applicable_claims():
    covered = _claim(id="a", expected_coverage="yes")
    uncovered = _claim(id="b", expected_coverage="no")
    results = [
        ClaimResult(claim=covered, predicted_verdict="Supported", predicted_confidence=0.9,
                    had_datapoint=True, latency_seconds=1.0),
        ClaimResult(claim=uncovered, predicted_verdict="Inconclusive", predicted_confidence=0.2,
                    had_datapoint=False, latency_seconds=1.0),
    ]
    report = compute_report(results)
    # Only the covered claim counts toward the denominator.
    assert report.retrieval_recall == 1.0


def test_compute_report_coverage_honesty_rate():
    uncovered_honest = _claim(id="a", expected_coverage="no")
    uncovered_dishonest = _claim(id="b", expected_coverage="no")
    results = [
        ClaimResult(claim=uncovered_honest, predicted_verdict="Inconclusive", predicted_confidence=0.2,
                    had_datapoint=False, latency_seconds=1.0),
        ClaimResult(claim=uncovered_dishonest, predicted_verdict="Contradicted", predicted_confidence=0.9,
                    had_datapoint=False, latency_seconds=1.0),
    ]
    report = compute_report(results)
    assert report.coverage_honesty_rate == 0.5


def test_compute_report_no_response_is_tracked_separately_from_wrong():
    claim = _claim()
    results = [ClaimResult(claim=claim, predicted_verdict=None, predicted_confidence=None,
                           had_datapoint=False, latency_seconds=None, error="boom")]
    report = compute_report(results)
    assert report.n_total == 1
    assert report.accuracy == 0.0
    domain_report = report.domains[claim.domain]
    assert domain_report.no_response == 1
    assert domain_report.wrong == 0


def test_compute_report_calibration_buckets_flag_unreliable_small_samples():
    claim = _claim()
    results = [ClaimResult(claim=claim, predicted_verdict="Supported", predicted_confidence=0.8,
                           had_datapoint=True, latency_seconds=1.0)]
    report = compute_report(results)
    bucket = next(b for b in report.calibration_buckets if b["confidence_bucket"] == "0.75-1.0")
    assert bucket["n"] == 1
    assert bucket["reliable"] is False
