"""Tests for the search.gov research harness: the client and content fetcher
that replaced Data.gov catalog search as the default research tier.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from api.search_gov import query_search_gov
from utils.content_fetch import fetch_and_extract, _strip_html, _cache


def _mock_client(payload):
    response = MagicMock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


# ------------------------------------------------------------- search.gov

@pytest.mark.asyncio
async def test_query_search_gov_requires_configured_keys():
    with patch("api.search_gov.SEARCH_GOV_AFFILIATE", None), \
         patch("api.search_gov.SEARCH_GOV_API_KEY", None):
        out = await query_search_gov("federal budget defense")
    assert out[0]["status"] == "failed"
    assert "not configured" in out[0]["error"]


@pytest.mark.asyncio
async def test_query_search_gov_empty_query_short_circuits():
    out = await query_search_gov("")
    assert out == []


@pytest.mark.asyncio
async def test_query_search_gov_fetches_content_for_top_results():
    payload = {"web": {"results": [
        {"title": "GAO Report on Defense Spending", "url": "https://gao.gov/report1", "snippet": "A report."},
        {"title": "CBO Analysis", "url": "https://cbo.gov/report2", "snippet": "Another report."},
    ]}}

    with patch("api.search_gov.SEARCH_GOV_AFFILIATE", "test-site"), \
         patch("api.search_gov.SEARCH_GOV_API_KEY", "test-key"), \
         patch("api.search_gov.httpx.AsyncClient", return_value=_mock_client(payload)), \
         patch("api.search_gov.fetch_and_extract", AsyncMock(return_value="Full report text here.")):
        out = await query_search_gov("defense spending report")

    assert len(out) == 2
    assert out[0]["content_excerpt"] == "Full report text here."
    assert out[0]["title"].startswith("Gov Research:")


@pytest.mark.asyncio
async def test_query_search_gov_degrades_gracefully_when_fetch_fails():
    """A page fetch failure must not drop the search result — snippet still usable."""
    payload = {"web": {"results": [
        {"title": "Some Report", "url": "https://agency.gov/x", "snippet": "Snippet text."},
    ]}}

    with patch("api.search_gov.SEARCH_GOV_AFFILIATE", "test-site"), \
         patch("api.search_gov.SEARCH_GOV_API_KEY", "test-key"), \
         patch("api.search_gov.httpx.AsyncClient", return_value=_mock_client(payload)), \
         patch("api.search_gov.fetch_and_extract", AsyncMock(return_value=None)):
        out = await query_search_gov("some query")

    assert len(out) == 1
    assert "content_excerpt" not in out[0]
    assert out[0]["snippet"] == "Snippet text."


@pytest.mark.asyncio
async def test_query_search_gov_handles_http_error():
    request = httpx.Request("GET", "https://search.gov/api/v2/search")
    response = httpx.Response(503, request=request, text="unavailable")
    client = MagicMock()
    client.get = AsyncMock(side_effect=httpx.HTTPStatusError("err", request=request, response=response))
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("api.search_gov.SEARCH_GOV_AFFILIATE", "test-site"), \
         patch("api.search_gov.SEARCH_GOV_API_KEY", "test-key"), \
         patch("api.search_gov.httpx.AsyncClient", return_value=ctx):
        out = await query_search_gov("query")

    assert out[0]["status"] == "failed"
    assert "503" in out[0]["error"]


@pytest.mark.asyncio
async def test_query_search_gov_skips_results_without_url():
    payload = {"web": {"results": [{"title": "No URL here", "snippet": "x"}]}}
    with patch("api.search_gov.SEARCH_GOV_AFFILIATE", "test-site"), \
         patch("api.search_gov.SEARCH_GOV_API_KEY", "test-key"), \
         patch("api.search_gov.httpx.AsyncClient", return_value=_mock_client(payload)):
        out = await query_search_gov("query")
    assert out == []


# --------------------------------------------------------- content fetcher

def test_strip_html_removes_scripts_and_tags():
    html = "<html><head><script>evil()</script></head><body><p>Hello <b>World</b></p></body></html>"
    assert _strip_html(html) == "Hello World"


@pytest.mark.asyncio
async def test_fetch_and_extract_returns_html_text():
    _cache.clear()
    html_body = b"<html><body><p>Federal spending rose in 2023.</p></body></html>"

    async def fake_aiter_bytes():
        yield html_body

    response = MagicMock()
    response.raise_for_status.return_value = None
    response.headers = {"content-type": "text/html"}
    response.aiter_bytes = fake_aiter_bytes

    stream_ctx = MagicMock()
    stream_ctx.__aenter__ = AsyncMock(return_value=response)
    stream_ctx.__aexit__ = AsyncMock(return_value=False)

    client = MagicMock()
    client.stream = MagicMock(return_value=stream_ctx)
    client_ctx = MagicMock()
    client_ctx.__aenter__ = AsyncMock(return_value=client)
    client_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("utils.content_fetch.httpx.AsyncClient", return_value=client_ctx):
        text = await fetch_and_extract("https://agency.gov/report")

    assert text == "Federal spending rose in 2023."


@pytest.mark.asyncio
async def test_fetch_and_extract_caches_result():
    """A second fetch of the same URL must not hit the network again."""
    _cache.clear()
    html_body = b"<p>Cached content.</p>"

    async def fake_aiter_bytes():
        yield html_body

    response = MagicMock()
    response.raise_for_status.return_value = None
    response.headers = {"content-type": "text/html"}
    response.aiter_bytes = fake_aiter_bytes

    stream_ctx = MagicMock()
    stream_ctx.__aenter__ = AsyncMock(return_value=response)
    stream_ctx.__aexit__ = AsyncMock(return_value=False)

    client = MagicMock()
    client.stream = MagicMock(return_value=stream_ctx)
    client_ctx = MagicMock()
    client_ctx.__aenter__ = AsyncMock(return_value=client)
    client_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("utils.content_fetch.httpx.AsyncClient", return_value=client_ctx) as mock_client_cls:
        first = await fetch_and_extract("https://agency.gov/cached")
        second = await fetch_and_extract("https://agency.gov/cached")

    assert first == second == "Cached content."
    assert mock_client_cls.call_count == 1, "second call should be served from cache"


@pytest.mark.asyncio
async def test_fetch_and_extract_returns_none_on_http_error():
    _cache.clear()
    request = httpx.Request("GET", "https://agency.gov/missing")
    response = httpx.Response(404, request=request)

    stream_ctx = MagicMock()
    stream_ctx.__aenter__ = AsyncMock(side_effect=httpx.HTTPStatusError("404", request=request, response=response))
    stream_ctx.__aexit__ = AsyncMock(return_value=False)

    client = MagicMock()
    client.stream = MagicMock(return_value=stream_ctx)
    client_ctx = MagicMock()
    client_ctx.__aenter__ = AsyncMock(return_value=client)
    client_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("utils.content_fetch.httpx.AsyncClient", return_value=client_ctx):
        result = await fetch_and_extract("https://agency.gov/missing")

    assert result is None


@pytest.mark.asyncio
async def test_fetch_and_extract_rejects_non_http_url():
    assert await fetch_and_extract("not-a-url") is None
    assert await fetch_and_extract("") is None


def test_extract_pdf_text_handles_real_pdf_bytes():
    """Round-trip a minimal real PDF through pypdf to confirm extraction works
    end-to-end, not just against mocks."""
    pypdf = pytest.importorskip("pypdf")
    from io import BytesIO
    from utils.content_fetch import _extract_pdf_text

    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = BytesIO()
    writer.write(buf)

    # A blank page has no text; this just proves the code path doesn't crash
    # on real PDF bytes and returns None rather than raising.
    result = _extract_pdf_text(buf.getvalue(), max_chars=1000)
    assert result is None
