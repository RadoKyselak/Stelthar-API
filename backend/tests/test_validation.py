"""Tests for input validation.

Regression coverage for two bugs found in a code-quality audit:
  * an SQL-injection keyword filter (guarding a codebase with no SQL/database
    layer anywhere) rejected common government-research vocabulary
    ("create", "drop", "select", "delete", "alter", "execute") outright.
  * claims were HTML-escaped server-side even though they're never rendered as
    raw HTML anywhere (the extension uses textContent), corrupting the actual
    claim text (e.g. "<" became "&lt;") before it ever reached the LLM.
"""
import pytest

from utils.validation import InputValidator, ValidationError


class TestSanitizeClaimAcceptsLegitimateGovernmentClaims:
    """These all failed before the fix — each contains a former SQL-filter keyword."""

    @pytest.mark.parametrize("claim", [
        "Congress voted to create a new federal agency in 2023.",
        "The administration moved to drop tariffs on steel imports.",
        "The Senate select committee released its report.",
        "The bill would delete funding for the program.",
        "Immigration policy changes could alter border crossings.",
        "The agency will execute the new regulation next year.",
        "The union proposed to update the wage schedule.",
        "Lawmakers want to insert a new provision into the bill.",
    ])
    def test_no_longer_rejected(self, claim):
        result = InputValidator.sanitize_claim(claim)
        assert result  # does not raise


class TestSanitizeClaimPreservesContent:
    def test_does_not_html_escape_special_characters(self):
        raw = "Defense spending exceeded $100B & education was <$50B in 2023."
        result = InputValidator.sanitize_claim(raw)
        assert "&amp;" not in result
        assert "&lt;" not in result
        assert "&" in result
        assert "<" in result

    def test_normalizes_whitespace(self):
        assert InputValidator.sanitize_claim("claim   with   gaps") == "claim with gaps"

    def test_strips_control_characters(self):
        result = InputValidator.sanitize_claim("claim\x00with\x07control chars")
        assert "\x00" not in result
        assert "\x07" not in result


class TestSanitizeClaimStillNeutralizesExecutableContent:
    """Defense-in-depth: neutralize rather than reject, since a real claim
    should never legitimately contain these constructs."""

    def test_strips_script_tags(self):
        result = InputValidator.sanitize_claim("The budget was <script>alert(1)</script> $5B.")
        assert "<script" not in result.lower()

    def test_strips_javascript_uri(self):
        result = InputValidator.sanitize_claim("See javascript:alert(1) for the report on spending.")
        assert "javascript:" not in result.lower()


class TestSanitizeClaimLengthValidation:
    def test_rejects_empty(self):
        with pytest.raises(ValidationError):
            InputValidator.sanitize_claim("")

    def test_rejects_too_short(self):
        with pytest.raises(ValidationError):
            InputValidator.sanitize_claim("ab")

    def test_rejects_too_long(self):
        with pytest.raises(ValidationError):
            InputValidator.sanitize_claim("a" * 5001)

    def test_rejects_whitespace_only_after_strip(self):
        with pytest.raises(ValidationError):
            InputValidator.sanitize_claim("   ")
