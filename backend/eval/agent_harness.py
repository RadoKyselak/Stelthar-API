"""Drive the dataset finder by hand, one tool call at a time.

In production a model runs the finder's tool loop (evidence/datasets/finder.py):
it reads a claim, calls the dataset tools, and names the figure that answers
it. Each run costs a little. To test and improve that step without spending,
this module lets anything else sit in the model's seat: a person, or an agent
given the same instructions and the same tools. The tools, the figures they
return and the verdict rules are the service's own; only the chooser differs.

    python -m eval.agent_harness brief                      # the instructions and tools
    python -m eval.agent_harness call S.json official_series '{"measure": "unemployment_rate", "year": 2012, "month": 1}'
    python -m eval.agent_harness answer S.json D1 --exact true --place "United States"

A session file holds the claim and how each figure was fetched (the same
"plan" production caches), so every command is its own process. `answer`
re-reads the chosen figure from the agency and judges the claim with
`dataset_tier.judge_found`, exactly as a production run would.

A harness result is not a production result: the chooser here may be a
stronger model than the one the service pays for. Use it to find what the
tools and the instructions cannot do, not to quote an accuracy figure.
"""
import argparse
import asyncio
import json
import logging
import pathlib
import sys
import time
from datetime import date
from typing import Any, Dict

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

MAX_CALLS = 12      # production: 5 rounds of at most 4 tools; the last round must answer


def _local_keys() -> None:
    """Agency keys for a run on this machine, from backend/.env if it exists
    (KEY=value lines; the file is not in the repository). The service itself
    reads its keys from the environment and is not changed by this."""
    import os
    env = pathlib.Path(__file__).resolve().parent.parent / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_local_keys()


def _load(path: str) -> Dict[str, Any]:
    return json.loads(pathlib.Path(path).read_text())


def _save(path: str, data: Dict[str, Any]) -> None:
    pathlib.Path(path).write_text(json.dumps(data, indent=1, ensure_ascii=False))


def brief() -> str:
    from evidence.datasets import finder, fred
    system = finder._SYSTEM.format(fred=finder._FRED_RULE if fred.available() else "")
    tools = [{"name": t["name"], "description": t["description"], "input": t["input_schema"]["properties"],
              "required": t["input_schema"].get("required", [])} for t in finder._tools()]
    return json.dumps({"instructions": system, "task": finder._TASK["check"], "tools": tools,
                       "max_tool_calls": MAX_CALLS,
                       }, indent=1, ensure_ascii=False)


async def call(path: str, tool: str, raw: str) -> str:
    from evidence.datasets import Candidate, finder
    from evidence.sources import SourceUnavailable
    data = _load(path)
    if len(data["log"]) >= MAX_CALLS:
        return json.dumps({"error": "no tool calls left; answer now"})
    try:
        inp = json.loads(raw)
    except ValueError:
        return json.dumps({"error": "input is not JSON"})
    s = finder._Session(data["claim"])
    s.fred_seen = {k: Candidate(**v) for k, v in data["fred_seen"].items()}
    fn = finder._RUN.get(tool)
    if fn is None:
        out: Any = {"error": "unknown tool"}
    else:
        try:
            out = await fn(s, inp if isinstance(inp, dict) else {})
        except SourceUnavailable as e:
            out = {"error": f"{e.source} is unreachable right now"}
        except Exception as e:  # noqa: BLE001 — as in production, a tool bug is an error result
            out = {"error": f"tool failed ({type(e).__name__})"}
    # This process numbered its figures from D1; the session numbers on.
    renamed = {}
    for key, (_, plan) in s.points.items():
        new = f"D{len(data['plans']) + 1}"
        data["plans"][new] = plan
        renamed[key] = new
    for item in (out if isinstance(out, list) else [out]):
        if isinstance(item, dict) and item.get("id") in renamed:
            item["id"] = renamed[item["id"]]
    for k, c in s.fred_seen.items():
        data["fred_seen"][k] = {"provider": c.provider, "id": c.id, "title": c.title, "units": c.units,
                                "frequency": c.frequency, "detail": c.detail, "meta": c.meta}
    text = json.dumps(out, ensure_ascii=False)[:finder._RESULT_CHARS]
    data["log"].append({"tool": tool, "input": inp, "result": text})
    _save(path, data)
    return text


async def answer(path: str, figure: str, exact: bool, place: str, why: str, claim_figure: str = "",
                 comparator: str = "", operation: str = "value", second: str = "") -> Dict[str, Any]:
    from claims.parser import parse_claim
    from evidence.datasets import finder
    from services import dataset_tier
    from services.verify_v2 import as_said, to_response_dict
    data = _load(path)
    text = data["claim"]
    said = date.fromisoformat(data["said_on"]) if data.get("said_on") else None
    out: Dict[str, Any] = {"id": data.get("id"), "claim": text, "said_on": data.get("said_on"),
                           "figure": figure, "exact": exact, "place": place, "why": why[:200],
                           "claim_figure": claim_figure, "comparator": comparator, "operation": operation,
                           "second": second,
                           "tool_calls": len(data["log"]), "verdict": "No verdict", "reason": "", "official": None}
    plan = data["plans"].get(figure.strip().upper())
    if plan is None:
        out["reason"] = "the chooser found no figure"
    else:
        dp = await finder.replay(plan, text)
        if dp is None:
            out["reason"] = "the figure could not be read again"
        else:
            out["official"] = {"value": dp.display_value(), "magnitude": dp.quantity.magnitude,
                               "raw": dp.raw_value, "measure": dp.label, "place": dp.geography.name,
                               "period": dp.observed.describe(), "source": dp.source, "series": dp.series_id}
            if finder.local_question_national_figure(dp, place):
                out["reason"] = "national figure for a local question"
            elif not claim_figure and text.rstrip().endswith("?"):
                out["reason"] = "a lookup: the figure is the answer, and nothing is judged"
            else:
                # The claim as the service would read it: with its period
                # written in when the day it was said gives one.
                dated = as_said(text, said)
                parsed = parse_claim(dated[0] if dated else text)
                reading = finder._reading({"claim_figure": claim_figure, "comparator": comparator,
                                           "operation": operation})
                other_plan = data["plans"].get(second.strip().upper()) if second else None
                other = await finder.replay(other_plan, text) if other_plan else None
                if reading["operation"] != "value" and other is None:
                    out["reason"] = "an operation with no second figure"
                else:
                    found = finder.Found(dp, exact, why, second=other if reading["operation"] != "value" else None,
                                         **reading)
                    result = dataset_tier.judge_found(text, parsed, found, time.monotonic())
                    if result is None:
                        out["reason"] = ("the figure was not accepted (not exact, the claim's figure not read, "
                                         "figures that cannot be combined, wrong place or period, or no verdict)")
                    else:
                        r = to_response_dict(result)
                        out["verdict"], out["reason"] = r["verdict"], r.get("reason") or ""
                        out["headline"] = r.get("headline", "")
    data["outcome"] = out
    _save(path, data)
    return out


def main() -> int:
    logging.disable(logging.WARNING)
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("brief")
    sub.add_parser("keys")
    new = sub.add_parser("new")
    new.add_argument("session"); new.add_argument("claim"); new.add_argument("--said-on", default="")
    new.add_argument("--id", default="")
    c = sub.add_parser("call")
    c.add_argument("session"); c.add_argument("tool"); c.add_argument("input")
    a = sub.add_parser("answer")
    a.add_argument("session"); a.add_argument("figure")
    a.add_argument("--exact", choices=["true", "false"], default="false")
    a.add_argument("--place", default="United States"); a.add_argument("--why", default="")
    a.add_argument("--claim-figure", default=""); a.add_argument("--second", default="")
    a.add_argument("--comparator", choices=["equals", "greater than", "less than"], default="equals")
    a.add_argument("--operation", default="value")
    args = ap.parse_args()
    if args.cmd == "keys":
        # Which sources this machine can reach. Names only, never a value.
        import config
        for name in ("FRED_API_KEY", "BLS_API_KEY", "BEA_API_KEY", "CENSUS_API_KEY"):
            print(f"{name}: {'set' if getattr(config, name, None) else 'not set'}")
    elif args.cmd == "brief":
        print(brief())
    elif args.cmd == "new":
        _save(args.session, {"id": args.id, "claim": args.claim, "said_on": args.said_on,
                             "plans": {}, "fred_seen": {}, "log": []})
        print("ok")
    elif args.cmd == "call":
        print(asyncio.run(call(args.session, args.tool, args.input)))
    else:
        print(json.dumps(asyncio.run(answer(args.session, args.figure, args.exact == "true", args.place, args.why,
                                            args.claim_figure, args.comparator, args.operation, args.second)),
                         indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
