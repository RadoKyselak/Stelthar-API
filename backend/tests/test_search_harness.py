"""Tests for the Tavily research harness: the client and content fetcher
that replaced Data.gov catalog search as the default research tier.

(Originally built against search.gov; switched to Tavily after search.gov's
affiliate registration proved not to be self-service — no instant API key.)
"""
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from api.tavily import query_tavily, _is_trusted_url
from utils.content_fetch import fetch_and_extract, _strip_html, _cache


def _mock_client(payload):
    response = MagicMock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


# ------------------------------------------------------------------ Tavily

@pytest.mark.asyncio
async def test_query_tavily_requires_configured_key():
    with patch("api.tavily.TAVILY_API_KEY", None):
        out = await query_tavily("federal budget defense")
    assert out[0]["status"] == "failed"
    assert "not configured" in out[0]["error"]


@pytest.mark.asyncio
async def test_query_tavily_empty_query_short_circuits():
    out = await query_tavily("")
    assert out == []


@pytest.mark.asyncio
async def test_query_tavily_uses_own_raw_content_without_extra_fetch():
    """Tavily fetches/extracts page text server-side (include_raw_content) —
    confirm we use it directly rather than re-fetching ourselves."""
    payload = {"results": [
        {"title": "GAO Report on Defense Spending", "url": "https://gao.gov/report1",
         "content": "A report.", "raw_content": "Full report text here.", "score": 0.9},
    ]}

    with patch("api.tavily.TAVILY_API_KEY", "test-key"), \
         patch("api.tavily.httpx.AsyncClient", return_value=_mock_client(payload)), \
         patch("api.tavily.fetch_and_extract", AsyncMock(return_value="SHOULD NOT BE CALLED")) as mock_fetch:
        out = await query_tavily("defense spending report")

    assert len(out) == 1
    assert out[0]["content_excerpt"] == "Full report text here."
    assert out[0]["title"].startswith("Web Research:")
    mock_fetch.assert_not_called()


@pytest.mark.asyncio
async def test_query_tavily_falls_back_to_own_fetch_when_raw_content_missing():
    payload = {"results": [
        {"title": "Some Report", "url": "https://agency.gov/x", "content": "Snippet.", "score": 0.5},
    ]}

    with patch("api.tavily.TAVILY_API_KEY", "test-key"), \
         patch("api.tavily.httpx.AsyncClient", return_value=_mock_client(payload)), \
         patch("api.tavily.fetch_and_extract", AsyncMock(return_value="Fetched via fallback.")) as mock_fetch:
        out = await query_tavily("some query")

    assert out[0]["content_excerpt"] == "Fetched via fallback."
    mock_fetch.assert_called_once()


@pytest.mark.asyncio
async def test_query_tavily_degrades_gracefully_when_all_content_fetch_fails():
    """A page fetch failure must not drop the search result — snippet still usable."""
    payload = {"results": [
        {"title": "Some Report", "url": "https://agency.gov/x", "content": "Snippet text.", "score": 0.5},
    ]}

    with patch("api.tavily.TAVILY_API_KEY", "test-key"), \
         patch("api.tavily.httpx.AsyncClient", return_value=_mock_client(payload)), \
         patch("api.tavily.fetch_and_extract", AsyncMock(return_value=None)):
        out = await query_tavily("some query")

    assert len(out) == 1
    assert "content_excerpt" not in out[0]
    assert out[0]["snippet"] == "Snippet text."


@pytest.mark.asyncio
async def test_query_tavily_ranks_trusted_domains_above_generic_web():
    """Unlike search.gov, Tavily searches the open web — .gov results should
    rank above a same-relevance-score generic site."""
    payload = {"results": [
        {"title": "Random Blog", "url": "https://someblog.example.com/post", "content": "x", "score": 0.5},
        {"title": "Official Report", "url": "https://cbo.gov/report", "content": "y", "score": 0.5},
    ]}

    with patch("api.tavily.TAVILY_API_KEY", "test-key"), \
         patch("api.tavily.httpx.AsyncClient", return_value=_mock_client(payload)), \
         patch("api.tavily.fetch_and_extract", AsyncMock(return_value=None)):
        out = await query_tavily("query")

    assert "cbo.gov" in out[0]["url"]


@pytest.mark.asyncio
async def test_query_tavily_handles_http_error():
    request = httpx.Request("POST", "https://api.tavily.com/search")
    response = httpx.Response(503, request=request, text="unavailable")
    client = MagicMock()
    client.post = AsyncMock(side_effect=httpx.HTTPStatusError("err", request=request, response=response))
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("api.tavily.TAVILY_API_KEY", "test-key"), \
         patch("api.tavily.httpx.AsyncClient", return_value=ctx):
        out = await query_tavily("query")

    assert out[0]["status"] == "failed"
    assert "503" in out[0]["error"]


@pytest.mark.asyncio
async def test_query_tavily_skips_results_without_url():
    payload = {"results": [{"title": "No URL here", "content": "x"}]}
    with patch("api.tavily.TAVILY_API_KEY", "test-key"), \
         patch("api.tavily.httpx.AsyncClient", return_value=_mock_client(payload)):
        out = await query_tavily("query")
    assert out == []


def test_is_trusted_url_recognizes_official_domains():
    assert _is_trusted_url("https://www.gao.gov/report") is True
    assert _is_trusted_url("https://cbo.gov/x") is True
    assert _is_trusted_url("https://sgp.fas.org/crs/report.pdf") is True
    assert _is_trusted_url("https://reuters.com/article") is True
    assert _is_trusted_url("https://randomblog.example.com") is False


# --------------------------------------------------------- content fetcher
# (unchanged by the search.gov -> Tavily switch, still exercised here since
# Tavily's fallback path depends on it)

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
