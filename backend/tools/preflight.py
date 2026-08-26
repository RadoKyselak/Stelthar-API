"""Live preflight: prove which credentials and endpoints actually work.

PLAN.md repeatedly notes things that were "implemented but never smoke-tested
against a live key" — the Tavily request shape most explicitly. That gap is how
a dead dependency survives in production unnoticed, which is exactly what
happened to the embedding model (text-embedding-004 was shut down 2026-01-14,
so the semantic-alignment half of the confidence score has been silently
returning 0.0 ever since).

This script makes one real request per dependency and reports what came back.
It is deliberately chatty and deliberately cheap: one small call each.

    set -a && source .env && set +a
    cd backend && python -m tools.preflight

Nothing here writes to the repo, and no key is ever printed.
"""
import asyncio
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TIMEOUT = 30.0

OK = "PASS"
BAD = "FAIL"
SKIP = "SKIP"


def _mask(key: Optional[str]) -> str:
    if not key:
        return "(unset)"
    return f"set, {len(key)} chars, ends …{key[-4:]}"


class Check:
    def __init__(self, name: str):
        self.name = name
        self.status = SKIP
        self.detail = ""
        self.payload: Any = None

    def ok(self, detail: str, payload: Any = None) -> "Check":
        self.status, self.detail, self.payload = OK, detail, payload
        return self

    def fail(self, detail: str, payload: Any = None) -> "Check":
        self.status, self.detail, self.payload = BAD, detail, payload
        return self

    def skip(self, detail: str) -> "Check":
        self.status, self.detail = SKIP, detail
        return self


async def check_gemini_generate(model: str) -> Check:
    c = Check(f"Gemini generateContent ({model})")
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        return c.skip("GEMINI_API_KEY unset")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {"contents": [{"role": "user", "parts": [{"text": "Reply with exactly: OK"}]}]}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.post(url, headers={"x-goog-api-key": key}, json=body)
        if r.status_code != 200:
            return c.fail(f"HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        text = data["candidates"][0]["content"]["parts"][0].get("text", "")
        return c.ok(f"replied {text.strip()[:40]!r}")
    except Exception as e:
        return c.fail(f"{type(e).__name__}: {e}")


async def check_gemini_embed(model: str) -> Check:
    """The check that matters most — a dead embedding model degrades silently."""
    c = Check(f"Gemini batchEmbedContents ({model})")
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        return c.skip("GEMINI_API_KEY unset")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:batchEmbedContents"
    body = {"requests": [{"model": f"models/{model}", "content": {"parts": [{"text": "unemployment rate"}]}}]}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.post(url, headers={"x-goog-api-key": key}, json=body)
        if r.status_code != 200:
            return c.fail(f"HTTP {r.status_code}: {r.text[:220]}")
        vals = r.json()["embeddings"][0]["values"]
        return c.ok(f"{len(vals)}-dim vector")
    except Exception as e:
        return c.fail(f"{type(e).__name__}: {e}")


async def check_gemini_grounding(model: str) -> Check:
    """Does google_search grounding work, and what does the metadata look like?"""
    c = Check(f"Gemini google_search grounding ({model})")
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        return c.skip("GEMINI_API_KEY unset")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "contents": [{"role": "user", "parts": [{
            "text": "What was the U.S. annual average unemployment rate in 2024 according to BLS?"
        }]}],
        "tools": [{"google_search": {}}],
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.post(url, headers={"x-goog-api-key": key}, json=body)
        if r.status_code != 200:
            return c.fail(f"HTTP {r.status_code}: {r.text[:220]}")
        cand = r.json()["candidates"][0]
        gm = cand.get("groundingMetadata") or {}
        chunks = gm.get("groundingChunks") or []
        supports = gm.get("groundingSupports") or []
        queries = gm.get("webSearchQueries") or []
        uris = [ch.get("web", {}).get("uri", "") for ch in chunks[:3]]
        return c.ok(
            f"{len(chunks)} chunks, {len(supports)} supports, queries={queries[:2]}",
            {"sample_uris": uris, "has_searchEntryPoint": bool(gm.get("searchEntryPoint"))},
        )
    except Exception as e:
        return c.fail(f"{type(e).__name__}: {e}")


async def check_bea() -> Check:
    c = Check("BEA NIPA T31600")
    key = os.getenv("BEA_API_KEY")
    if not key:
        return c.skip("BEA_API_KEY unset")
    params = {
        "UserID": key, "method": "GetData", "DataSetName": "NIPA",
        "TableName": "T31600", "Frequency": "A", "Year": "2023",
        "LineCode": "2", "ResultFormat": "JSON",
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.get("https://apps.bea.gov/api/data", params=params)
        data = r.json()
        results = data.get("BEAAPI", {}).get("Results", {})
        if "Error" in results or "Error" in data.get("BEAAPI", {}):
            err = results.get("Error") or data["BEAAPI"].get("Error")
            return c.fail(f"BEA error: {str(err)[:200]}")
        rows = results.get("Data", [])
        if not rows:
            return c.fail("no Data rows returned")
        row = rows[0]
        return c.ok(
            f"{row.get('LineDescription')} = {row.get('DataValue')} "
            f"(UNIT_MULT={row.get('UNIT_MULT')}, CL_UNIT={row.get('CL_UNIT')})",
            {"unit_mult": row.get("UNIT_MULT"), "cl_unit": row.get("CL_UNIT")},
        )
    except Exception as e:
        return c.fail(f"{type(e).__name__}: {e}")


async def check_bls() -> Check:
    c = Check("BLS timeseries LNS14000000")
    key = os.getenv("BLS_API_KEY")
    if not key:
        return c.skip("BLS_API_KEY unset")
    body = {
        "seriesid": ["LNS14000000"], "startyear": "2023", "endyear": "2024",
        "registrationKey": key, "annualaverage": True,
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.post(
                "https://api.bls.gov/publicAPI/v2/timeseries/data/",
                headers={"Content-Type": "application/json"}, content=json.dumps(body),
            )
        data = r.json()
        if data.get("status") != "REQUEST_SUCCEEDED":
            return c.fail(f"{data.get('status')}: {data.get('message')}")
        pts = data["Results"]["series"][0]["data"]
        m13 = [p for p in pts if p.get("period") == "M13"]
        return c.ok(
            f"{len(pts)} points; {len(m13)} annual averages (M13); "
            f"latest {pts[0].get('year')}-{pts[0].get('period')}={pts[0].get('value')}",
            {"m13_present": bool(m13)},
        )
    except Exception as e:
        return c.fail(f"{type(e).__name__}: {e}")


async def check_census() -> Check:
    c = Check("Census ACS1 median household income (TX)")
    key = os.getenv("CENSUS_API_KEY")
    if not key:
        return c.skip("CENSUS_API_KEY unset")
    params = {"get": "NAME,B19013_001E", "for": "state:48", "key": key}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.get("https://api.census.gov/data/2023/acs/acs1", params=params)
        if r.status_code != 200:
            return c.fail(f"HTTP {r.status_code}: {r.text[:200]}")
        rows = r.json()
        return c.ok(f"{rows[1][0]} = {rows[1][1]}")
    except Exception as e:
        return c.fail(f"{type(e).__name__}: {e}")


async def check_treasury() -> Check:
    c = Check("Treasury debt_to_penny (keyless)")
    url = ("https://api.fiscaldata.treasury.gov/services/api/fiscal_service"
           "/v2/accounting/od/debt_to_penny")
    params = {"fields": "record_date,record_fiscal_year,tot_pub_debt_out_amt",
              "sort": "-record_date", "page[size]": "1", "format": "json"}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.get(url, params=params)
        rows = r.json().get("data", [])
        if not rows:
            return c.fail("no rows")
        return c.ok(f"{rows[0]['record_date']} = {rows[0]['tot_pub_debt_out_amt']}")
    except Exception as e:
        return c.fail(f"{type(e).__name__}: {e}")


async def check_usaspending() -> Check:
    c = Check("USAspending toptier_agencies (keyless)")
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.get("https://api.usaspending.gov/api/v2/references/toptier_agencies/")
        results = r.json().get("results", [])
        return c.ok(f"{len(results)} agencies listed")
    except Exception as e:
        return c.fail(f"{type(e).__name__}: {e}")


async def check_congress() -> Check:
    c = Check("Congress.gov bill search")
    key = os.getenv("CONGRESS_API_KEY")
    if not key:
        return c.skip("CONGRESS_API_KEY unset")
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.get("https://api.congress.gov/v3/bill",
                                 params={"api_key": key, "limit": 1, "format": "json"})
        if r.status_code != 200:
            return c.fail(f"HTTP {r.status_code}: {r.text[:200]}")
        bills = r.json().get("bills", [])
        return c.ok(f"{len(bills)} bill(s); latest {bills[0].get('title', '')[:60]!r}" if bills else "empty")
    except Exception as e:
        return c.fail(f"{type(e).__name__}: {e}")


async def check_tavily() -> Check:
    """PLAN.md flags this exact request shape as never having been smoke-tested."""
    c = Check("Tavily /search (body api_key)")
    key = os.getenv("TAVILY_API_KEY")
    if not key:
        return c.skip("TAVILY_API_KEY unset")
    body = {"api_key": key, "query": "BLS unemployment rate 2024 annual average",
            "search_depth": "basic", "max_results": 3, "include_raw_content": True}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.post("https://api.tavily.com/search", json=body)
        if r.status_code != 200:
            # Retry with the Authorization-header form Tavily moved to.
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                body2 = {k: v for k, v in body.items() if k != "api_key"}
                r2 = await client.post("https://api.tavily.com/search",
                                       headers={"Authorization": f"Bearer {key}"}, json=body2)
            if r2.status_code == 200:
                n = len(r2.json().get("results", []))
                return c.fail(
                    f"body api_key REJECTED (HTTP {r.status_code}) but Authorization: Bearer WORKS "
                    f"({n} results) — api/tavily.py sends the wrong form"
                )
            return c.fail(f"both forms failed: body={r.status_code}, bearer={r2.status_code} {r2.text[:150]}")
        results = r.json().get("results", [])
        has_raw = sum(1 for x in results if x.get("raw_content"))
        return c.ok(f"{len(results)} results, {has_raw} with raw_content")
    except Exception as e:
        return c.fail(f"{type(e).__name__}: {e}")


async def main() -> int:
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    embed_model = os.getenv("GEMINI_EMBEDDING_MODEL", "text-embedding-004")

    print("=" * 78)
    print("STELTHAR PREFLIGHT — one live request per dependency")
    print("=" * 78)
    for name in ("GEMINI_API_KEY", "BEA_API_KEY", "CENSUS_API_KEY", "BLS_API_KEY",
                 "CONGRESS_API_KEY", "DATA_GOV_API_KEY", "TAVILY_API_KEY"):
        print(f"  {name:<20} {_mask(os.getenv(name))}")
    print(f"  {'GEMINI_MODEL':<20} {model}")
    print(f"  {'EMBEDDING_MODEL':<20} {embed_model}")
    print()

    checks: List[Check] = await asyncio.gather(
        check_gemini_generate(model),
        check_gemini_embed(embed_model),
        check_gemini_grounding(model),
        check_bea(),
        check_bls(),
        check_census(),
        check_treasury(),
        check_usaspending(),
        check_congress(),
        check_tavily(),
    )

    print(f"{'RESULT':<6} {'CHECK':<42} DETAIL")
    print("-" * 78)
    for c in checks:
        print(f"{c.status:<6} {c.name:<42} {c.detail}")
        if c.payload:
            print(f"{'':<49} {c.payload}")

    failed = [c for c in checks if c.status == BAD]
    skipped = [c for c in checks if c.status == SKIP]
    print("-" * 78)
    print(f"{len(checks) - len(failed) - len(skipped)} passed, {len(failed)} failed, {len(skipped)} skipped")

    # A dead embedding model is the silent one — call it out explicitly.
    embed = next(c for c in checks if c.name.startswith("Gemini batchEmbedContents"))
    if embed.status == BAD:
        print()
        print("!! The embedding model is NOT reachable. SemanticAlignmentScorer will return")
        print("!! DEFAULT_S_SEMANTIC (0.0) for every request, so the 'semantic_alignment'")
        print("!! component of confidence — 45% of the weighted score — carries no evidence")
        print("!! signal at all and merely restates the LLM's own verdict.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
