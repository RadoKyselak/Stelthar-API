"""The extension checks a whole highlighted passage (extension 2.2).

Highlighting an article and right-clicking sends its text to /v2/check, with
no new permission: the text arrives through the context menu, so the extension
still never runs on a page (see test_extension_permissions).

The panel's helpers run in Node; skipped where Node is not installed.
"""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

EXT = pathlib.Path(__file__).resolve().parents[2] / "extension"
BG = re.sub(r"/\*.*?\*/|//[^\n]*", "", (EXT / "background.js").read_text(), flags=re.S)
PANEL = EXT / "panel.js"

node = pytest.mark.skipif(shutil.which("node") is None, reason="Node is not installed")


def run_panel(expr: str):
    """Evaluate `expr` with panel.js loaded (its page wiring never fires)."""
    script = (
        "const vm = require('vm'), fs = require('fs');"
        "global.window = { MiradorView: {} };"
        "global.document = { addEventListener() {} };"
        f"vm.runInThisContext(fs.readFileSync({json.dumps(str(PANEL))}, 'utf8'));"
        f"console.log(JSON.stringify({expr}));"
    )
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


class TestBackground:
    def test_a_check_goes_to_the_passage_endpoint(self):
        assert "`${API}/v2/check`" in BG and "postJSON(CHECK_URL, { text }" in BG

    def test_it_falls_back_when_the_server_has_no_passage_endpoint(self):
        """Rolling the feature back on the server must not break 2.2 clients."""
        check = BG[BG.index("async function checkText"):BG.index("const checkDeeper")]
        assert "e.status !== 404 && e.status !== 405" in check
        assert "postJSON(LEGACY_URL, { claim: text.slice(0, 1000) }" in check
        assert "claims: [{" in check              # the old answer, in the passage shape

    def test_search_further_runs_the_full_check_for_one_claim(self):
        assert "postJSON(VERIFY_URL, { claim }" in BG and 'message.type === "deeper"' in BG

    def test_an_outage_is_never_saved(self):
        check = BG[BG.index("async function checkText"):BG.index("const checkDeeper")]
        assert "isOutage(c.result)" in check
        assert check.index("isOutage") < check.index("setCachedResult")

    def test_the_limit_message_states_no_number(self):
        """It said "100 checks/day" while the server allowed 300."""
        assert not re.search(r"\d+ checks/day", BG)

    def test_saved_answers_from_before_are_dropped(self):
        """Their shape changed (one result -> a passage); a new key drops them."""
        assert 'CACHE_KEY = "stelthar_cache_v3"' in BG

    def test_version(self):
        assert json.loads((EXT / "manifest.json").read_text())["version"] == "2.2.0"


@node
class TestPanelHelpers:
    def test_text_within_the_cap_is_sent_whole(self):
        got = run_panel("fitPassage('Unemployment was 3.6% in 2023.')")
        assert got == {"text": "Unemployment was 3.6% in 2023.", "cut": False}

    def test_text_over_the_cap_is_cut_at_a_sentence(self):
        got = run_panel("(() => { const t = 'Unemployment was 3.6% in 2023. '.repeat(400);"
                        " const f = fitPassage(t); return [f.cut, f.text.length <= 8000, f.text.slice(-5)]; })()")
        assert got == [True, True, "2023."]

    def test_text_with_no_sentence_end_is_cut_at_the_cap(self):
        got = run_panel("(() => { const f = fitPassage('x'.repeat(9000)); return [f.cut, f.text.length]; })()")
        assert got == [True, 8000]

    def test_when_a_saved_check_was_made(self):
        got = run_panel("[whenChecked(Date.now()), whenChecked(Date.now() - 86400000), whenChecked(0)]")
        assert got == ["today", "yesterday", ""]
