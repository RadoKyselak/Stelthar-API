"""The verdict-from-evidence step (services/evidence_judge.py) and its
benchmark runner (eval/quantemp_judge.py). No model is called here."""
import json

import pytest

from eval import quantemp_judge as qj
from services import evidence_judge as ej


class TestWhatTheJudgeMayRead:
    def test_a_fact_checkers_ruling_is_never_shown(self):
        got = ej.usable(["politifact rated this claim mostly false after reviewing the numbers here",
                         "snopes: the photo does not show what the caption says it shows at all",
                         "a washington post fact-checker gave it three pinocchios for the claim",
                         "the bls reported the unemployment rate at 4.1 percent in october 2026"])
        assert [e.text for e in got] == ["the bls reported the unemployment rate at 4.1 percent in october 2026"]

    def test_repeats_and_scraps_are_dropped_and_the_order_kept(self):
        got = ej.usable(["first useful snippet about the budget deficit in 2023", "short",
                         "first useful snippet about the budget deficit in 2023",
                         "second useful snippet about federal spending in 2023"], limit=5)
        assert [e.text[:6] for e in got] == ["first ", "second"]

    def test_at_most_the_limit(self):
        assert len(ej.usable([f"snippet number {i} with enough words to count here" for i in range(50)], limit=30)) == 30


class TestTheInput:
    def test_official_figures_are_set_apart_from_snippets(self):
        text = ej.render("Unemployment was 4.1% in October 2026.",
                         [ej.Evidence("Unemployment rate, October 2026: 4.1%", "BLS LNS14000000", official=True),
                          ej.Evidence("some news snippet about jobs in the fall of the year")],
                         said_on="2026-11-07")
        assert text.splitlines()[:4] == [
            "Claim: Unemployment was 4.1% in October 2026.", "Said on: 2026-11-07",
            "Official figures found by this service (exact, from the agency):",
            "  * Unemployment rate, October 2026: 4.1% [BLS LNS14000000]"]
        assert "Search snippets (1):" in text

    def test_no_evidence_is_said_plainly(self):
        assert ej.render("x", []).endswith("Search snippets: none")


class TestTheReply:
    @pytest.mark.parametrize("reply, verdict", [
        ('{"verdict": "False", "why": "x"}', "False"),
        ('Sure.\n{"verdict": "conflicting", "why": "partly right"}', "Conflicting"),
        ('{"verdict": "true"}', "True"),
    ])
    def test_a_verdict_is_read(self, reply, verdict):
        got = ej.read(reply)
        assert got["verdict"] == verdict and got["service_verdict"] == ej.AS_SERVICE[verdict]

    @pytest.mark.parametrize("reply", ["", "no json here", '{"verdict": "Maybe"}', "{not json}"])
    def test_anything_else_is_no_verdict(self, reply):
        assert ej.read(reply) is None

    def test_the_instructions_never_ask_for_a_lookup(self):
        assert "never look anything up" in ej.INSTRUCTIONS
        assert "silence is NOT evidence" in ej.INSTRUCTIONS


class TestScoring:
    def write(self, tmp_path, name, rows):
        p = tmp_path / name
        p.write_text("\n".join(json.dumps(r) for r in rows))
        return str(p)

    def test_macro_f1_and_an_unrated_claim_counts_as_wrong(self, tmp_path):
        gold = self.write(tmp_path, "set.jsonl", [{"id": "a", "label": "True"}, {"id": "b", "label": "False"},
                                                   {"id": "c", "label": "Conflicting"}, {"id": "d", "label": "False"}])
        got = self.write(tmp_path, "v.jsonl", [{"id": "a", "verdict": "True"}, {"id": "b", "verdict": "False"},
                                                {"id": "c", "verdict": "Conflicting"}])
        s = qj.score(gold, [got])
        assert s["claims"] == 4 and s["rated"] == 3 and s["accuracy"] == pytest.approx(0.75)
        assert s["True"] == 1.0 and s["Conflicting"] == 1.0 and s["False"] == pytest.approx(2 / 3)

    def test_the_date_politifact_puts_in_its_address(self):
        assert qj.said_on("https://www.politifact.com/factchecks/2012/feb/08/x/y/") == "2012-02-08"
        assert qj.said_on("https://www.snopes.com/fact-check/x/") == ""
