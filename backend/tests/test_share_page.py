"""GET /c?claim=...: a claim's check as a page that can be sent (routes/share.py).

What matters here is what a stranger can make the page do: it must never
spend a paid call, never run text from the address as markup, and never show
a verdict the check did not reach.
"""
import json
import re
from unittest.mock import patch

import pytest

from claims.verdict import HIGH, LOW, NO_SERIES, NO_VERDICT, SUPPORTED
from routes import share
from services.verify_v2 import VerificationV2

HTML = {"accept": "text/html"}


@pytest.fixture(autouse=True)
def fresh_counters():
    from utils import spend_guard
    spend_guard._reset_for_tests()
    yield
    spend_guard._reset_for_tests()


def engine(verdict=SUPPORTED, headline="Claim says 3.6%. BLS reports 3.6% for 2023."):
    calls = []

    async def fake(claim, allow_grounding=True, client_ip=None):
        calls.append((claim, allow_grounding))
        return VerificationV2(claim=claim, verdict=verdict, headline=headline, tier="structured",
                              confidence=HIGH if verdict != NO_VERDICT else LOW,
                              reason=NO_SERIES if verdict == NO_VERDICT else None)
    return fake, calls


def get(test_client, claim, fake):
    with patch.object(share, "verify", fake):
        return test_client.get("/c", params={"claim": claim}, headers=HTML, follow_redirects=False)


class TestSharedCheck:
    def test_the_answer_is_in_the_page_and_its_preview(self, test_client):
        fake, calls = engine()
        r = get(test_client, "Unemployment was 3.6% in 2023", fake)
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
        assert "<title>Supported: “Unemployment was 3.6% in 2023” · Project Mirador</title>" in r.text
        assert 'property="og:title" content="Supported: “Unemployment was 3.6% in 2023”"' in r.text
        assert 'property="og:description" content="Claim says 3.6%. BLS reports 3.6% for 2023."' in r.text
        block = re.search(r'<script type="application/json" id="shared">(.*?)</script>', r.text, re.S)
        assert json.loads(block.group(1))["display"]["verdict"]["key"] == "supported"
        assert 'value="Unemployment was 3.6% in 2023"' in r.text

    def test_it_never_reaches_the_paid_tiers(self, test_client):
        fake, calls = engine()
        get(test_client, "Unemployment was 3.6% in 2023", fake)
        assert calls == [("Unemployment was 3.6% in 2023", False)]

    def test_an_unsettled_claim_shows_no_verdict_and_waits_for_the_visitor(self, test_client):
        fake, _ = engine(NO_VERDICT, "None of the official data series we check covers this claim.")
        r = get(test_client, "About 2 million people attended the parade", fake)
        assert r.status_code == 200 and 'id="shared"' not in r.text
        assert "<title>Check: “About 2 million people attended the parade”" in r.text
        assert 'value="About 2 million people attended the parade"' in r.text

    def test_the_verdict_cannot_be_written_into_the_address(self, test_client):
        fake, _ = engine(NO_VERDICT)
        with patch.object(share, "verify", fake):
            r = test_client.get("/c", params={"claim": "The moon is cheese 100%", "verdict": "Supported"},
                                headers=HTML, follow_redirects=False)
        assert "Supported" not in r.text.split("</head>")[0]

    def test_text_from_the_address_is_never_markup(self, test_client):
        fake, _ = engine()
        claim = 'Unemployment was 3.6% "><img src=x onerror=alert(1)> </script><script>alert(2)</script>'
        r = get(test_client, claim, fake)
        assert "<img" not in r.text and "alert(2)</script>" not in r.text
        block = re.search(r'<script type="application/json" id="shared">(.*?)</script>', r.text, re.S)
        assert "<" not in block.group(1)              # nothing inside can close the block

    def test_pages_are_kept_out_of_search_results(self, test_client):
        fake, _ = engine()
        assert '<meta name="robots" content="noindex">' in get(test_client, "GDP grew 2.9% in 2023", fake).text

    @pytest.mark.parametrize("claim", ["", "ab", "x" * 601])
    def test_nothing_to_show_goes_home(self, test_client, claim):
        fake, calls = engine()
        r = get(test_client, claim, fake)
        assert r.status_code == 302 and r.headers["location"] == "/" and calls == []

    def test_it_counts_against_the_visitors_allowance(self, test_client, monkeypatch):
        import config
        monkeypatch.setattr(config, "VERIFY_PER_MINUTE", 1)
        fake, calls = engine()
        codes = [get(test_client, "GDP grew 2.9% in 2023", fake).status_code for _ in range(2)]
        assert codes == [200, 429] and len(calls) == 1


class TestTemplate:
    def test_every_hook_in_the_home_page_is_still_there(self):
        """page() raises if index.html loses a tag it rewrites; this is the
        test that fails first when someone edits that file."""
        html = share.page("GDP grew 2.9% in 2023", None)
        assert "<title>Check: “GDP grew 2.9% in 2023” · Project Mirador</title>" in html

    def test_a_long_claim_is_cut_at_a_word_in_the_title(self):
        html = share.page("word " * 60, None)
        title = re.search(r"<title>(.*?)</title>", html).group(1)
        assert len(title) < 150 and "…" in title
