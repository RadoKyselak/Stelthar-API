"""Which text the extension panel checks as a claim and which it looks up.

Runs extension/result-view.js in Node; skipped where Node is not installed.
"""
import json
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
VIEW = ROOT / "extension" / "result-view.js"

CASES = {
    # Claims: a figure, a sentence, a yes/no question.
    "Unemployment in Texas was 4.1% in 2023": "check",
    "Vaccines cause autism": "check",
    "The Great Wall of China is visible from space": "check",
    "Is the federal minimum wage $7.25?": "check",
    "Did Congress pass the CHIPS Act in 2022?": "check",
    ("2026 Recalls of Food Products Associated with Fresh Jalapeños from Sinaloa, "
     "Mexico and distributed by Coast Citrus Distributors due to the Potential Risk "
     "of Salmonella"): "check",
    # Lookups: a measure and a period, or a what/how question.
    "unemployment texas 2023": "find",
    "poverty rate 2023": "find",
    "median household income california": "find",
    "what was unemployment in 2023": "find",
    "how much is the federal debt": "find",
}


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is not installed")
def test_intent():
    script = (
        "global.window = {};"
        f"require({json.dumps(str(VIEW))});"
        f"const cases = {json.dumps(list(CASES))};"
        "console.log(JSON.stringify(cases.map((c) => window.MiradorView.intentOf(c))));"
    )
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    got = dict(zip(CASES, json.loads(out.stdout)))
    assert got == CASES


def test_site_copy_matches_extension():
    assert (ROOT / "backend" / "static" / "result-view.js").read_text() == VIEW.read_text()
