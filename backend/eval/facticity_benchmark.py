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
    python -m eval.facticity_benchmark page eval/results/facticity_benchmark.json static/benchmarks.html
"""
import argparse
import html
import json
import sys
from typing import Dict, List

AS_ANSWER = {"SUPPORTED": "TRUE", "MOSTLY_ACCURATE": "TRUE", "CONTRADICTED": "FALSE", "MISLEADING": "FALSE"}
OTHERS = ("facticity", "perplexity", "bing")
NAMES = {"mirador": "Project Mirador", "facticity": "Facticity", "perplexity": "Perplexity", "bing": "Bing"}


def answer(verdict: str) -> str:
    return AS_ANSWER.get((verdict or "").strip().upper(), "Can't verify")


def accuracy(rows: List[Dict], system: str) -> float:
    return round(100 * sum(r[system] == r["truth"] for r in rows) / len(rows), 1)


def score(claims: List[Dict], verdicts: Dict[int, str]) -> Dict:
    rows = [{**c, "mirador": answer(verdicts.get(c["n"], ""))} for c in claims]
    return {"source": "Facticity, “Updated and Expanded Originality Dataset Benchmark”, 20 June 2024",
            "rows": rows,
            "accuracy": {s: accuracy(rows, s) for s in ("mirador",) + OTHERS}}


def page(data: Dict) -> str:
    acc = data["accuracy"]
    order = sorted(acc, key=lambda s: -acc[s])
    bars = "\n".join(
        f'      <li{" class=\"bench-us\"" if s == "mirador" else ""}><span class="bench-name">{NAMES[s]}</span>'
        f'<meter min="0" max="100" value="{acc[s]:.1f}"></meter><span class="bench-score">{acc[s]:.0f}%</span></li>'
        for s in order)
    cols = ["mirador"] + list(OTHERS)
    head = "".join(f'<th{" class=\"us\"" if s == "mirador" else ""}>{NAMES[s]}</th>' for s in cols)

    def cell(r, s):
        v = r[s]
        cls = "v" + (" miss" if v != r["truth"] else "") + (" us" if s == "mirador" else "")
        return f'<td class="{cls}">{html.escape(v)}</td>'
    body = "\n".join(
        f'          <tr><td class="v">{r["n"]}</td><td>{html.escape(r["claim"])}</td><td class="v">{r["truth"]}</td>'
        + "".join(cell(r, s) for s in cols) + "</tr>" for r in data["rows"])
    n = len(data["rows"])
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Project Mirador benchmark</title>
  <meta name="description" content="Project Mirador on Facticity's {n}-claim benchmark, against Facticity, Perplexity and Bing.">
  <meta property="og:type" content="website">
  <meta property="og:site_name" content="Project Mirador">
  <meta property="og:url" content="https://stelthar-api.vercel.app/benchmarks">
  <meta property="og:title" content="Project Mirador benchmark">
  <meta property="og:description" content="Project Mirador on Facticity's {n}-claim benchmark, against Facticity, Perplexity and Bing.">
  <meta property="og:image" content="https://stelthar-api.vercel.app/static/og.png">
  <meta property="og:image:width" content="1200">
  <meta property="og:image:height" content="630">
  <meta name="twitter:card" content="summary_large_image">
  <link rel="icon" href="/static/favicon.png" type="image/png">
  <link rel="stylesheet" href="/static/site.css">
</head>
<body>
  <main class="doc">
    <p class="doc-back"><a href="/">&larr; Check a claim</a></p>
    <h1>Benchmark</h1>
    <p class="bench-lede">The {n} claims Facticity uses to benchmark itself. Accuracy: the share of claims each system rated correctly as true or false.</p>
    <ol class="bench">
{bars}
    </ol>
    <p class="bench-note">Claims, ground truth and the Facticity, Perplexity and Bing answers are from {html.escape(data["source"])}. An answer that is neither true nor false counts as wrong, as in Facticity&rsquo;s table.</p>
    <div class="bench-table-wrap">
      <table class="bench-table">
        <thead><tr><th>#</th><th>Claim</th><th>Truth</th>{head}</tr></thead>
        <tbody>
{body}
        </tbody>
      </table>
    </div>
  </main>
  <footer class="foot">Independent fact checker · Not a government website · <a href="/about">About</a> · <a href="/methodology">Method</a> · <a href="/benchmarks">Benchmarks</a> · <a href="/developers">Developers</a> · <a href="/privacy">Privacy</a></footer>
</body>
</html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("score"); s.add_argument("claims"); s.add_argument("verdicts", nargs="+")
    p = sub.add_parser("page"); p.add_argument("results"); p.add_argument("out")
    a = ap.parse_args()
    if a.cmd == "score":
        got = {}
        for path in a.verdicts:
            for line in open(path):
                if line.strip():
                    r = json.loads(line); got[int(r["n"])] = r.get("verdict", "")
        json.dump(score(json.load(open(a.claims)), got), sys.stdout, ensure_ascii=False, indent=1)
        print()
    else:
        open(a.out, "w").write(page(json.load(open(a.results))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
