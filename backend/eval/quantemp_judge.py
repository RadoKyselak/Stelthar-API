"""QuanTemp, judged by services/evidence_judge.py, without a model bill.

QuanTemp (Venktesh et al., SIGIR 2024) is 2,495 test claims, each with the
search snippets the published systems were scored on. Its metric is
macro-F1 over True / False / Conflicting; a claim left unrated counts as
wrong. The best published system without leaked evidence scores 58.32.

    python -m eval.quantemp_judge split CLAIMS EVIDENCE OUTDIR [--dev 150] [--test 300]
    python -m eval.quantemp_judge score OUTDIR/<set>.jsonl OUTDIR/<set>-verdicts*.jsonl
    python -m eval.quantemp_judge resolve REPLIES.jsonl VERDICTS.jsonl     # decomposed replies -> verdicts

`split` draws a development set and a disjoint test set at random (fixed
seed) and writes, for each claim, what the judge reads: the instructions'
input with the claim's evidence. A batch of those can be put to anything
that reads instructions: a production model call or, for a free run, an
agent. `score` compares the verdicts with the fact-checkers' labels.

Neither QuanTemp file is in the repository (CC BY-NC 4.0). Get them from
github.com/factiverse/QuanTemp: data/raw_data/test_claims_quantemp.json and
data/bm25_scored_evidence/bm25_top_100_claimdecomp.json.zip.

Snippets that name a fact-checker are removed before the judge sees them
(see evidence_judge.usable). The published systems kept them, so this is the
stricter setting.
"""
import argparse
import json
import pathlib
import random
import re
import sys
from typing import Dict, List

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from services import evidence_judge  # noqa: E402

SEED = 20261118
_MON = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}


def said_on(url: str) -> str:
    """A PolitiFact fact-check's address carries the day it ran, close to
    the day the claim was made. Other outlets' addresses do not."""
    m = re.search(r"/(\d{4})/([a-z]{3})/(\d{1,2})/", url or "")
    return f"{m.group(1)}-{_MON[m.group(2)]:02d}-{int(m.group(3)):02d}" if m and m.group(2) in _MON else ""


def split(claims_path: str, evidence_path: str, out: str, dev: int, test: int,
          skip: int = 0, snippets: int = 30) -> None:
    claims = json.load(open(claims_path))
    evidence = json.load(open(evidence_path))
    assert len(claims) == len(evidence)
    order = list(range(len(claims)))
    random.Random(SEED).shuffle(order)
    # `skip` leaves out claims an earlier test drew (the first dev + skip of
    # the same shuffle), so a fresh test never reuses them.
    sets = {"dev": order[:dev], "test": order[dev + skip:dev + skip + test]}
    outdir = pathlib.Path(out)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "INSTRUCTIONS.txt").write_text(evidence_judge.INSTRUCTIONS)
    (outdir / "DECOMPOSE_INSTRUCTIONS.txt").write_text(evidence_judge.DECOMPOSE_INSTRUCTIONS)
    for name, idx in sets.items():
        with open(outdir / f"{name}.jsonl", "w") as f:
            for i in idx:
                c = claims[i]
                ev = evidence_judge.usable(evidence[i]["docs"], limit=snippets)
                f.write(json.dumps({"id": f"t{i}", "label": c["label"], "said_on": said_on(c.get("url", "")),
                                    "input": evidence_judge.render(c["claim"], ev, said_on(c.get("url", "")))},
                                   ensure_ascii=False) + "\n")
        print(f"{name}: {len(idx)} claims -> {outdir / (name + '.jsonl')}")


def f1s(pairs: List[tuple]) -> Dict[str, float]:
    out = {}
    for c in evidence_judge.VERDICTS:
        tp = sum(1 for p, g in pairs if p == c and g == c)
        pp = sum(1 for p, _ in pairs if p == c)
        gp = sum(1 for _, g in pairs if g == c)
        pr, rc = (tp / pp if pp else 0.0), (tp / gp if gp else 0.0)
        out[c] = 2 * pr * rc / (pr + rc) if pr + rc else 0.0
    n = len(pairs)
    weights = {c: sum(1 for _, g in pairs if g == c) / n for c in evidence_judge.VERDICTS}
    out["macro"] = sum(out[c] for c in evidence_judge.VERDICTS) / 3
    out["weighted"] = sum(out[c] * weights[c] for c in evidence_judge.VERDICTS)
    out["accuracy"] = sum(1 for p, g in pairs if p == g) / n
    return out


def score(set_path: str, verdict_paths: List[str]) -> Dict[str, float]:
    gold = {}
    for line in open(set_path):
        row = json.loads(line)
        gold[row["id"]] = row["label"]
    got = {}
    for p in verdict_paths:
        for line in open(p):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("id") in gold and row.get("verdict") in evidence_judge.VERDICTS:
                got[row["id"]] = row["verdict"]
    # A claim left unrated is wrong: it is scored as no class at all.
    pairs = [(got.get(i, "unrated"), g) for i, g in gold.items()]
    s = f1s(pairs)
    s["claims"], s["rated"] = len(gold), len(got)
    return s


def resolve(replies: str, out: str) -> None:
    """Decomposed replies ({"id", "questions", "lean"} per line) to verdicts,
    worked out by evidence_judge.decide."""
    n = 0
    with open(out, "w") as f:
        for line in open(replies):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            got = evidence_judge.read_decomposed(json.dumps(row))
            if got is not None:
                f.write(json.dumps({"id": row.get("id"), **got}, ensure_ascii=False) + "\n")
                n += 1
    print(f"{n} verdicts -> {out}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("split")
    a.add_argument("claims"); a.add_argument("evidence"); a.add_argument("out")
    a.add_argument("--dev", type=int, default=150); a.add_argument("--test", type=int, default=300)
    a.add_argument("--skip", type=int, default=0); a.add_argument("--snippets", type=int, default=30)
    r = sub.add_parser("resolve")
    r.add_argument("replies"); r.add_argument("out")
    b = sub.add_parser("score")
    b.add_argument("set"); b.add_argument("verdicts", nargs="+")
    args = ap.parse_args()
    if args.cmd == "split":
        split(args.claims, args.evidence, args.out, args.dev, args.test, args.skip, args.snippets)
    elif args.cmd == "resolve":
        resolve(args.replies, args.out)
    else:
        s = score(args.set, args.verdicts)
        print(json.dumps({k: (round(v * 100, 2) if isinstance(v, float) else v) for k, v in s.items()}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
