"""EconWebArena's U.S. agency questions through the free lookup.

EconWebArena (Liu & Quan, 2025, arXiv:2506.08136, Apache-2.0) is 360 questions
with exact numeric answers and the page each was read from. 102 are about
U.S. agencies: 75 ask for a published statistic (BLS, BEA, Census, the
Federal Reserve, Treasury, EIA, HUD, the Department of Labor) and 27 for a
rule or a fee (IRS thresholds, Medicare premiums, USCIS fees), which is not
what this service looks up.

    python -m eval.econwebarena path/to/econ_web_arena_test.csv [out.json]

The file is not in the repository; it is at
huggingface.co/datasets/EconWebArena/EconWebArena. Rules only, no model call.

A figure is right when it equals the benchmark's answer to the answer's own
precision. The benchmark's answers were read in spring 2025, and agencies
revise: a figure that differs from the answer may be today's revised one.
The run cannot tell those apart by itself, so it lists every mismatch.
"""
import asyncio, csv, json, logging, re, sys, pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import eval.agent_harness  # noqa: E402,F401 — reads agency keys from backend/.env when it exists
logging.disable(logging.CRITICAL)
from services.lookup import lookup  # noqa: E402

US = ("U.S. ", "Federal Reserve", "California Franchise", "New York Department of Labor")

ADMIN = ("California Franchise", "New York Department", "Citizenship", "Internal Revenue", "Medicare",
         "Securities and Exchange", "Small Business", "Social Security")

def question(task):
    """The question as a person would type it: the benchmark's instruction about answer format removed."""
    return re.split(r"\s+Provide only\b", task)[0].strip()

def matches(got, want):
    try:
        w = float(want)
    except ValueError:
        return False
    digits = len(want.split(".")[1]) if "." in want else 0
    tol = 0.5 * 10 ** (-digits) + 1e-9
    for scale in ((1,) if abs(got) < 1e4 else (1, 1e3, 1e6, 1e9, 1e12)):
        if abs(got / scale - w) <= tol or abs(abs(got) / scale - abs(w)) <= tol:
            return True
    return False

async def main():
    rows = [r for r in csv.DictReader(open(IN, newline="", encoding="utf-8-sig")) if r["website"].startswith(US)]
    out = []
    for r in rows:
        q = question(r["task"])
        try:
            res = await lookup(q, deep=False)
            ev = res.evidence or {}
            raw = ev.get("raw_value") if isinstance(ev, dict) else None
            mag = None
            for cand in (ev.get("value") if isinstance(ev, dict) else None, raw):
                try:
                    mag = float(cand); break
                except (TypeError, ValueError):
                    pass
            o = {"ok": res.ok, "value": res.value, "magnitude": mag, "label": res.label, "reason": res.reason,
                 "period": (ev.get("period_label") or ev.get("period") or "") if isinstance(ev, dict) else "",
                 "place": ev.get("geography", "") if isinstance(ev, dict) else "", "headline": res.headline[:200]}
        except Exception as e:
            o = {"ok": False, "value": None, "magnitude": None, "label": None, "reason": f"error {type(e).__name__}", "period": "", "place": "", "headline": ""}
        o.update(id=r["id"], site=r["website"], q=q, want=r["answer"], kind="admin" if r["website"].replace("U.S. ", "").startswith(ADMIN) else "stat")
        o["right"] = bool(o["ok"] and o["magnitude"] is not None and matches(o["magnitude"], r["answer"]))
        out.append(o)
    if OUT:
        json.dump(out, open(OUT, "w"), indent=1)
    def tally(rs):
        n = len(rs); a = sum(o["ok"] for o in rs); c = sum(o["right"] for o in rs)
        return f"{n} questions | answered {a} | right {c} | wrong {a - c} | declined {n - a}"
    print("ALL :", tally(out)); print("STAT:", tally([o for o in out if o["kind"] == "stat"])); print("ADMN:", tally([o for o in out if o["kind"] == "admin"]))
    for o in out:
        if o["ok"]:
            print(("  RIGHT " if o["right"] else "  WRONG ") + f"{o['id']:>4} want {o['want']:>10} got {o['value']} [{o['label']}; {o['place']}; {o['period']}] | {o['q'][45:150]}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    IN, OUT = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "")
    asyncio.run(main())
