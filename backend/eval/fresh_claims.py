"""Recent fact-checked claims: a test no model can have memorized.

QuanTemp's claims were fact-checked before 2024, so a large model may have read
the rulings while it was trained. This set is PolitiFact's rulings published
after a cutoff date, read from PolitiFact's public WordPress API, so the
answers did not exist when the models were built.

    python -m eval.fresh_claims fetch OUT.json [--after 2026-07-01]
    python -m eval.fresh_claims score OUT.json VERDICTS.jsonl...

Labels follow QuanTemp: True and Mostly True -> True; Half True and Mostly
False -> Conflicting; False and Pants on Fire -> False. A ruling is read from
the article's "We rate it ..." sentence; articles without one are left out.
The set is not stored in the repository (PolitiFact's text).

Measured 18 Nov 2026 on 110 rulings from 1 Jul to 18 Nov 2026, the judge
(services/evidence_judge.INSTRUCTIONS) reading Mirador's evidence (web
search with fact-checker sites blocked, and official figures) against the
same model given only the claim (macro-F1):

    extra reasoning   69.2 with Mirador's evidence, 46.4 alone (+22.8; 95% +8.6 to +34.1)
    fast              57.7 with Mirador's evidence, 54.0 alone
"""
import argparse
import html
import json
import re
import sys
import urllib.request
from typing import Dict, List

API = "https://www.politifact.com/wp-json/wp/v2/factcheck_post"
_RULING = re.compile(r"\b[Ww]e rate (?:it|this|that|the (?:claim|statement|post|posts))?[^.]{0,40}?"
                     r"\b(pants on fire|mostly false|mostly true|half[- ]true|false|true)\b", re.IGNORECASE)
LABEL = {"pants on fire": "False", "false": "False", "mostly false": "Conflicting", "half true": "Conflicting",
         "half-true": "Conflicting", "mostly true": "True", "true": "True"}


def ruling(text: str) -> str:
    found = _RULING.findall(text)
    return LABEL.get(found[-1].lower(), "") if found else ""


def _text(rendered: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", rendered or "")).strip()


def fetch(after: str) -> List[Dict]:
    rows, page = [], 1
    while True:
        url = (f"{API}?per_page=100&page={page}&after={after}T00:00:00"
               "&_fields=date,link,excerpt,content,class_list")
        req = urllib.request.Request(url, headers={"User-Agent": "mirador-eval"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                batch = json.load(r)
        except Exception:  # noqa: BLE001 — past the last page the API answers 400
            break
        if not isinstance(batch, list) or not batch:
            break
        for x in batch:
            label = ruling(_text(x["content"]["rendered"]))
            if not label:
                continue
            who = [k.split("personalities-", 1)[1] for k in x.get("class_list", []) if k.startswith("personalities-")]
            rows.append({"date": x["date"][:10], "link": x["link"], "claim": " ".join(_text(x["excerpt"]["rendered"]).split()),
                         "speaker": ", ".join(w.replace("-", " ").title() for w in who) or "unknown", "label": label})
        page += 1
    rows.sort(key=lambda r: r["link"])
    for i, r in enumerate(rows):
        r["id"] = f"f{i:03d}"
    return rows


def main() -> int:
    from eval.quantemp_judge import f1s
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch"); f.add_argument("out"); f.add_argument("--after", default="2026-07-01")
    s = sub.add_parser("score"); s.add_argument("claims"); s.add_argument("verdicts", nargs="+")
    a = ap.parse_args()
    if a.cmd == "fetch":
        rows = fetch(a.after)
        json.dump(rows, open(a.out, "w"), ensure_ascii=False, indent=0)
        print(f"{len(rows)} rated claims -> {a.out}")
        return 0
    gold = {r["id"]: r["label"] for r in json.load(open(a.claims))}
    got = {}
    for p in a.verdicts:
        for line in open(p):
            if line.strip():
                r = json.loads(line); got[r["id"]] = r.get("verdict")
    out = f1s([(got.get(i, "unrated"), g) for i, g in gold.items()])
    print(json.dumps({k: round(v * 100, 2) for k, v in out.items()}, indent=1))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
    raise SystemExit(main())
