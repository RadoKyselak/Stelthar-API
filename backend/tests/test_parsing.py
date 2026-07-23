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
