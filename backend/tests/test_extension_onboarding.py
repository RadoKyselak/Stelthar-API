"""Onboarding (extension 2.3): a short walkthrough on install, and one rating ask.

  welcome.html  opened once, on a first install (never on update): practise
                the one move, highlight a sentence and right-click it. Both
                steps tick as they happen. No pinning step. No new permission: tabs.create, action,
                commands and windows need none.
  rating ask    in the side panel, after the first check with a verdict. "Not
                now" asks once more ten verdicts later; then never again. No
                reward is offered (Chrome Web Store policy).
"""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

EXT = pathlib.Path(__file__).resolve().parents[2] / "extension"
strip = lambda s: re.sub(r"/\*.*?\*/|//[^\n]*", "", s, flags=re.S)
BG = strip((EXT / "background.js").read_text())
PANEL = (EXT / "panel.js").read_text()
WELCOME_JS = (EXT / "welcome.js").read_text()
WELCOME = (EXT / "welcome.html").read_text()

node = pytest.mark.skipif(shutil.which("node") is None, reason="Node is not installed")


class TestWelcome:
    def test_opens_on_first_install_only(self):
        hook = BG[BG.index("runtime.onInstalled"):BG.index("contextMenus.onClicked")]
        assert 'details.reason === "install"' in hook
        assert 'chrome.tabs.create({ url: chrome.runtime.getURL("welcome.html") })' in hook

    def test_two_steps_highlight_then_right_click(self):
        assert WELCOME.count('class="step"') == 2
        assert WELCOME.index('id="step-highlight"') < WELCOME.index('id="step-check"')
        assert "pin" not in WELCOME.lower() and "getUserSettings" not in WELCOME_JS
        assert "Fact-check with Mirador" in WELCOME          # the menu item's own words
        assert 'title: "Fact-check with Mirador"' in BG
        assert 'id="sample"' in WELCOME and 'src="welcome.js"' in WELCOME

    def test_steps_tick_when_they_really_happen(self):
        assert '"selectionchange"' in WELCOME_JS and "containsNode(sample" in WELCOME_JS
        assert '"stelthar_history"' in WELCOME_JS

    def test_the_fallback_opens_the_panel_inside_the_click(self):
        click = WELCOME_JS[WELCOME_JS.index('$("try").addEventListener'):]
        assert click.index("sidePanel.open") < click.index("storage.session.set")
        assert "await" not in click.split("});")[0]

    def test_no_script_inline(self):
        assert not re.search(r"<script(?![^>]*\bsrc=)", WELCOME)


class TestRatingAsk:
    def test_links_to_the_store_reviews(self):
        assert re.search(r'REVIEW_URL = "https://chromewebstore\.google\.com/detail/[^"]+/anehbncclhojcaihgeoebennaccpeegn/reviews"', PANEL)

    def test_asked_only_after_a_verdict(self):
        assert "if (hasVerdict(result)) maybeAskForRating(wrap);" in PANEL

    def test_no_reward_offered(self):
        assert not re.search(r"\b(reward|unlock|free month|discount|bonus)\b", PANEL, re.I)


@node
def test_when_the_rating_is_asked():
    script = (
        "const vm = require('vm'), fs = require('fs');"
        "global.window = { MiradorView: { verdictKey: (k) => String(k).toLowerCase() } };"
        "global.document = { addEventListener() {} };"
        f"vm.runInThisContext(fs.readFileSync({json.dumps(str(EXT / 'panel.js'))}, 'utf8'));"
        "const v = (k) => ({ claims: [{ result: { display: { verdict: { key: k } } } }] });"
        "console.log(JSON.stringify([shouldAsk(undefined), shouldAsk({ later: 0 }), shouldAsk({ later: 9 }),"
        " shouldAsk({ later: 10 }), shouldAsk({ done: true }),"
        " hasVerdict(v('supported')), hasVerdict(v('contradicted')), hasVerdict(v('no-verdict')),"
        " hasVerdict(v('unavailable')), hasVerdict({ claims: [] })]));"
    )
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == [True, False, False, True, False, True, True, False, False, False]
