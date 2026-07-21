"""Tests for request-context middleware, specifically the X-Request-ID guard.

Regression coverage: the client-supplied X-Request-ID header used to be trusted
verbatim — logged and echoed back in a response header with no validation,
which is a log-injection / oversized-header vector.
"""
from unittest.mock import MagicMock

from middleware.context import _resolve_request_id, _SAFE_REQUEST_ID


def _request_with_header(value):
    req = MagicMock()
    req.headers = {"X-Request-ID": value} if value is not None else {}
    return req


def test_accepts_well_formed_client_id():
    result = _resolve_request_id(_request_with_header("trace-abc123_XYZ"))
    assert result == "trace-abc123_XYZ"


def test_rejects_id_with_control_characters():
    malicious = "id\r\nSet-Cookie: evil=1"
    result = _resolve_request_id(_request_with_header(malicious))
    assert result != malicious
    assert _SAFE_REQUEST_ID.match(result)  # falls back to a generated safe UUID


def test_rejects_oversized_id():
    result = _resolve_request_id(_request_with_header("x" * 500))
    assert len(result) < 500


def test_generates_id_when_absent():
    result = _resolve_request_id(_request_with_header(None))
    assert _SAFE_REQUEST_ID.match(result)


def test_rejects_id_with_spaces():
    result = _resolve_request_id(_request_with_header("has spaces in it"))
    assert result != "has spaces in it"
    assert _SAFE_REQUEST_ID.match(result)
