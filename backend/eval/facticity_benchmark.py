"""Facticity's own benchmark, with Mirador added as a column.

Facticity published a table of 120 claims (Originality.ai's fact-check
dataset, "updated and expanded" by Facticity, 20 June 2024): each claim's
ground truth, TRUE or FALSE, and the answer of Facticity, Perplexity and
Bing, with accuracy at the foot (92%, 88%, 78%). An answer that is not TRUE
or FALSE ("Inconclusive") counts as wrong.

Mirador answers each claim through its live check (the web-search step,
evidence/claude_search.py) and its verdict is read the way the service shows
it:

    SUPPORTED, MOSTLY_ACCURATE   -> TRUE
    CONTRADICTED, MISLEADING     -> FALSE
    NO_EVIDENCE, NOT_EMPIRICAL   -> Can't verify (counts as wrong)

    python -m eval.facticity_benchmark score CLAIMS.json MIRADOR_VERDICTS.jsonl... > results.json

Mirador checks claims about government records, so it is scored on the
claims in that focus: settled by what a government or official public body
publishes or decides (statistics, office-holders, laws and official acts,
court and Justice Department actions, agency programs and records, health
agencies' findings, intergovernmental bodies). Each claim was sorted by an
agent that saw only the claim's text, before any score was looked at
("in_focus" in the results file). Every system is scored on the same claims.
"""
import argparse
import json
import sys
from typing import Dict, List

AS_ANSWER = {"SUPPORTED": "TRUE", "MOSTLY_ACCURATE": "TRUE", "CONTRADICTED": "FALSE", "MISLEADING": "FALSE"}
OTHERS = ("facticity", "perplexity", "bing")


def answer(verdict: str) -> str:
    return AS_ANSWER.get((verdict or "").strip().upper(), "Can't verify")


def accuracy(rows: List[Dict], system: str) -> float:
    return round(100 * sum(r[system] == r["truth"] for r in rows) / len(rows), 1)


def in_focus(rows: List[Dict]) -> List[Dict]:
    return [r for r in rows if r.get("in_focus")]


def score(claims: List[Dict], verdicts: Dict[int, str]) -> Dict:
    rows = [{**c, "mirador": answer(verdicts.get(c["n"], ""))} for c in claims]
    return {"source": "Facticity, “Updated and Expanded Originality Dataset Benchmark”, 20 June 2024",
            "rows": rows,
            "accuracy": {s: accuracy(rows, s) for s in ("mirador",) + OTHERS}}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("score"); s.add_argument("claims"); s.add_argument("verdicts", nargs="+")
    a = ap.parse_args()
    if a.cmd == "score":
        got = {}
        for path in a.verdicts:
            for line in open(path):
                if line.strip():
                    r = json.loads(line); got[int(r["n"])] = r.get("verdict", "")
        json.dump(score(json.load(open(a.claims)), got), sys.stdout, ensure_ascii=False, indent=1)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
