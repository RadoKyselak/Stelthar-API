import pytest
from utils.parsing import (
    extract_json_block,
    parse_numeric_value as _parse_numeric_value,
    apply_multiplier as _apply_multiplier,
)


class TestExtractJsonBlock:
    """Tests for extract_json_block function."""
    
    def test_valid_json(self):
        """Test extracting valid JSON from text."""
        text = 'Some text before {"key": "value", "num": 123} some text after'
        result = extract_json_block(text)
        assert result == {"key": "value", "num": 123}
    
    def test_nested_json(self):
        """Test extracting nested JSON."""
        text = 'Text {"outer": {"inner": "value"}} more'
        result = extract_json_block(text)
        assert result == {"outer": {"inner": "value"}}
    
    def test_no_json(self):
        """Test with no JSON in text."""
        text = "No JSON here at all"
        result = extract_json_block(text)
        assert result is None
    
    def test_empty_string(self):
        """Test with empty string."""
        result = extract_json_block("")
        assert result is None
    
    def test_none_input(self):
        """Test with None input."""
        result = extract_json_block(None)
        assert result is None
    
    def test_invalid_json_fallback(self):
        """Test with malformed JSON."""
        text = 'Text {"key": "value", "bad": } end'
        result = extract_json_block(text)
        assert result is None
    
    def test_json_with_control_characters(self):
        """Test JSON with control characters (should clean them)."""
        text = '{"key": "value\x00\x01\x02"}'
        result = extract_json_block(text)
        assert result is not None
        assert "key" in result

class TestParseNumericValue:
    def test_simple_integer(self):
        """Test parsing simple integer."""
        assert _parse_numeric_value("123") == 123.0
    
    def test_simple_float(self):
        """Test parsing simple float."""
        assert _parse_numeric_value("123.456") == 123.456
    
    def test_with_commas(self):
        """Test parsing number with commas."""
        assert _parse_numeric_value("1,234,567.89") == 1234567.89
    
    def test_with_dollar_sign(self):
        """Test parsing number with dollar sign."""
        assert _parse_numeric_value("$1,234.56") == 1234.56
    
    def test_negative_parentheses(self):
        """Test parsing negative number in parentheses."""
        assert _parse_numeric_value("(123.45)") == -123.45
    
    def test_scientific_notation(self):
        """Test parsing scientific notation."""
        assert _parse_numeric_value("1.23e5") == 123000.0
    
    def test_none_input(self):
        """Test with None input."""
        assert _parse_numeric_value(None) is None
    
    def test_empty_string(self):
        """Test with empty string."""
        assert _parse_numeric_value("") is None
    
    def test_invalid_format(self):
        """Test with invalid format."""
        assert _parse_numeric_value("not a number") is None
    
    def test_whitespace(self):
        """Test with whitespace around number."""
        assert _parse_numeric_value("  123.45  ") == 123.45

class TestApplyMultiplier:
    """Tests for _apply_multiplier function."""
    
    def test_apply_multiplier(self):
        """Test applying multiplier to value."""
        assert _apply_multiplier(1000, 1000000) == 1000000000.0
    
    def test_none_multiplier(self):
        """Test with None multiplier (should return original value)."""
        assert _apply_multiplier(1234.56, None) == 1234.56
    
    def test_none_value(self):
        """Test with None value."""
        assert _apply_multiplier(None, 1000) is None
    
    def test_string_multiplier(self):
        """Test with string multiplier (should convert)."""
        assert _apply_multiplier(100, "1000") == 100000.0
    
    def test_invalid_multiplier(self):
        """Test with invalid multiplier."""
        assert _apply_multiplier(100, "invalid") == 100


class TestYearsThatNameEvents:
    """"GDP fell 3.7% following the 1973 oil crisis" is not about calendar 1973."""

    @pytest.mark.parametrize("text", [
        "GDP fell 3.7% following the 1973 oil crisis",
        "Unemployment peaked at 10% after the 2008 financial crisis",
        "The 2017 tax cuts added $1.9 trillion to the deficit",
        "Following 2020, unemployment fell to 3.5%",
    ])
    def test_the_period_is_left_unresolved(self, text):
        from claims.parser import parse_claim
        p = parse_claim(text)
        assert p.year is None and p.has_unresolved_period is True

    @pytest.mark.parametrize("text,year", [
        ("Unemployment was 9.3% in 2009 after the 2008 financial crisis", 2009),
        ("Unemployment was 3.6% in 2023", 2023),
        ("The unemployment rate hit 14.8% in April 2020 during the pandemic", 2020),
    ])
    def test_a_period_stated_plainly_still_counts(self, text, year):
        from claims.parser import parse_claim
        p = parse_claim(text)
        assert p.year == year and p.has_unresolved_period is False


class TestQualifiersTheSeriesCannotHonour:
    @pytest.mark.parametrize("text,word", [
        ("The unemployment rate of young African Americans was 28.2% in May 2013", "young"),
        ("Manufacturing added 12,000 jobs in May 2024", "one industry"),
        ("Professional and business services has added 269,000 jobs since October 2025", "one industry"),
        ("The economy has added 15 million jobs since January 2021", "since a date"),
    ])
    def test_the_claim_is_declined_not_misread(self, text, word):
        from claims.parser import parse_claim
        assert word in parse_claim(text).structured_blocker

    @pytest.mark.parametrize("text", [
        "The economy added 3 million jobs in 2023",
        "The unemployment rate for Black workers was 5.3% in 2023",
    ])
    def test_what_the_series_does_measure_is_untouched(self, text):
        from claims.parser import parse_claim
        assert parse_claim(text).structured_blocker == ""


class TestAMonthWithoutItsYear:
    def test_with_no_year_the_period_is_unresolved(self):
        from claims.parser import parse_claim
        p = parse_claim("The unemployment rate rose to 4.6% in November")
        assert p.year is None and p.month is None and p.has_unresolved_period is True

    def test_with_one_year_elsewhere_it_is_that_years_month(self):
        from claims.parser import parse_claim
        p = parse_claim("In 2025, the unemployment rate rose to 4.6% in November")
        assert (p.year, p.month) == (2025, 11)

    def test_may_the_verb_is_not_a_month(self):
        from claims.parser import parse_claim
        p = parse_claim("The unemployment rate may rise to 5% in 2027")
        assert p.month is None
