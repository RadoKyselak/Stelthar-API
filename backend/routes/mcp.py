"""POST /mcp: the checks as tools for an assistant (Model Context Protocol).

An assistant asked "is that true?" answers from what it remembers, and what it
remembers about last month's inflation is whatever it last read. This endpoint
gives it the agency's own figure instead, with the series and a citation, the
same way the extension gets it.

The protocol is MCP's streamable HTTP transport in its simplest form: each
request is one JSON-RPC message and each answer one JSON body. There is no
session, no stream and nothing kept between calls, which is what a serverless
function can honestly offer.

Four tools, all free:

  check_text    every claim in a passage, each on its own
  check_page    the same, for a public web page given its address
  check_claim   one claim
  find_figure   one official figure, with its citation

None of them can reach the paid tiers. A person pressing a button may ask for
the full check; a program calling in a loop may not, so every tool answers
from agency data only and says so when that was not enough.

A tool's answer is plain text an assistant can quote, plus the same facts as
structured content. Failures inside a tool (a limit reached, text too long)
come back as a tool result marked `isError`, as the protocol asks, so the
assistant can read what went wrong.
"""
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

import config
from config import logger
from services.check_link import check_link
from services.check_text import check_text
from services.lookup import lookup, to_response_dict as lookup_to_dict
from utils.request_limits import client_ip, retry_after
from utils.safe_fetch import FetchFailed, FetchRefused
from utils.validation import InputValidator, ValidationError

router = APIRouter()

# Newest first. A client that asks for a version we know gets it back;
# anything else gets the newest, and decides for itself.
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "project-mirador", "title": "Project Mirador", "version": "2.2.0"}
INSTRUCTIONS = (
    "Project Mirador checks claims against official U.S. government data (BLS, BEA, Census, Treasury, "
    "Congress.gov, FBI, CDC, FDA, FTC, HUD, USDA; World Bank for other countries) and returns the agency's "
    "figure with its source. Use check_text for a passage, check_page for a web page, check_claim for one statement, find_figure to "
    "look a number up. Quote the figure, its period and its source as returned; when there is no verdict, "
    "say so rather than guessing."
)

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "check_text",
        "title": "Check every claim in a passage",
        "description": ("Cut a passage (up to 8,000 characters) into its factual claims and check each "
                        "against official U.S. government data. Returns a verdict per claim (Supported, "
                        "Accurate when published, Mostly accurate, Contradicted, Misleading, or Can't verify), "
                        "the official figure, its period and source."),
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "The passage to check."}},
            "required": ["text"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "check_page",
        "title": "Check the claims on a web page",
        "description": ("Fetch a public web page (a press release, a transcript, an article) by its address "
                        "and check the claims on it that official U.S. government data can settle. Returns a "
                        "verdict per claim with the official figure and source. Some sites refuse automated "
                        "readers; the answer then says so."),
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "The page's address (http or https)."}},
            "required": ["url"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "check_claim",
        "title": "Check one claim",
        "description": ("Check one factual claim, such as \"Unemployment in Texas was 4.1% in 2023\", against "
                        "official U.S. government data. Returns the verdict, the official figure, how the "
                        "claim was read (measure, place, period), the source and a citation."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "claim": {"type": "string", "description": "One sentence stating a fact."},
                "said_on": {"type": "string", "description": "The day the claim was made, YYYY-MM-DD, if known. "
                            "A claim that names no period is then held to the figure that was newest that day."},
            },
            "required": ["claim"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "find_figure",
        "title": "Find an official figure",
        "description": ("Look up one official U.S. statistic, such as \"unemployment rate texas 2023\" or "
                        "\"national debt\". Returns the figure, its period, the agency and a citation."),
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "A measure, and optionally a place and a period."}},
            "required": ["query"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
]


class ToolError(Exception):
    """Something the caller can fix; its message is the tool's answer."""


# ---------------------------------------------------------------- answers

def _brief(result: Dict[str, Any]) -> Dict[str, Any]:
    """One check, as the handful of facts worth quoting."""
    d = result["display"]
    comps = [c for c in (d.get("comparisons") or []) if c.get("official")]
    c = comps[0] if comps else {}
    source = next((s for s in (d.get("sources") or []) if s.get("url")), {})
    out = {
        "claim": result.get("claim"),
        "verdict": d["verdict"]["label"],
        "meaning": d["verdict"].get("meaning"),
        "summary": d.get("summary"),
        "official_figure": c.get("official") or d.get("reported_value"),
        "measure": c.get("measure"),
        "place": (c.get("place") or "United States") if c else None,
        "period": c.get("period"),
        "revision": (d.get("revision") or {}).get("text"),
        "confidence": (d.get("confidence") or {}).get("tier"),
        "why_no_verdict": (d.get("reason") or {}).get("why"),
        "source": source.get("agency") or source.get("domain"),
        "source_url": source.get("url"),
        "citation": (d.get("citation") or "").split("\n")[0],
        "checked_on": d.get("checked_on"),
        "page": d.get("share_url"),
    }
    return {k: v for k, v in out.items() if v}


def _lines(b: Dict[str, Any]) -> str:
    parts = [f"{b['verdict']}: “{b.get('claim', '')}”"]
    if b.get("official_figure"):
        where = ", ".join(x for x in (b.get("measure"), b.get("place"), b.get("period")) if x)
        label = "Official figure today" if b.get("revision") else "Official figure"
        parts.append(f"{label}: {b['official_figure']}" + (f" ({where})" if where else ""))
    for key in ("revision", "why_no_verdict"):
        if b.get(key):
            parts.append(b[key])
    if not b.get("official_figure") and b.get("summary") and not b.get("why_no_verdict"):
        parts.append(b["summary"])
    if b.get("source"):
        parts.append(f"Source: {b['source']}" + (f" {b['source_url']}" if b.get("source_url") else ""))
    return "\n".join(parts)


async def _limited(kind: str, request: Request) -> None:
    wait = await retry_after(kind, client_ip(request))
    if wait is not None:
        raise ToolError(f"Too many requests from this connection. Try again in {wait} seconds.")


def _text_arg(arguments: Dict[str, Any], name: str, limit: int, passage: bool = False) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"“{name}” must be a non-empty string.")
    try:
        if passage:
            return InputValidator.sanitize_passage(value, limit)
        value = InputValidator.sanitize_claim(value)
    except ValidationError as e:
        raise ToolError(str(e))
    if len(value) > limit:
        raise ToolError(f"“{name}” cannot exceed {limit} characters.")
    return value


async def _check_text(arguments: Dict[str, Any], request: Request) -> Dict[str, Any]:
    text = _text_arg(arguments, "text", config.CHECK_MAX_CHARS, passage=True)
    await _limited("check", request)
    got = await check_text(text, client_ip=client_ip(request), free_only=True)
    claims = [_brief(e["result"]) | ({"as_written": e["span"]} if e["span"] != e["claim"] else {})
              for e in got["claims"]]
    tally = {k: v for k, v in got["tally"].items() if v}
    head = f"{len(claims)} claim{'s' if len(claims) != 1 else ''} checked against official data."
    if got["truncated"]:
        head += " More claims were in the text than one request checks; send the rest separately."
    return {"text": "\n\n".join([head] + [_lines(c) for c in claims]),
            "structured": {"claims": claims, "tally": tally, "truncated": got["truncated"]}}


async def _check_page(arguments: Dict[str, Any], request: Request) -> Dict[str, Any]:
    url = arguments.get("url")
    if not isinstance(url, str) or not url.strip() or len(url) > 2000:
        raise ToolError("\u201curl\u201d must be a web address.")
    if not config.CHECK_LINKS:
        raise ToolError("Checking a page by its address is switched off. Send its text to check_text instead.")
    await _limited("check", request)
    try:
        got = await check_link(url, client_ip=client_ip(request))
    except (FetchRefused, FetchFailed) as e:
        raise ToolError(str(e))
    claims = [_brief(e["result"]) | ({"as_written": e["span"]} if e["span"] != e["claim"] else {})
              for e in got["claims"]]
    page = got["page"]
    name = page.get("title") or page["host"]
    head = (f"{len(claims)} claim{'s' if len(claims) != 1 else ''} on \u201c{name}\u201d ({page['host']}) could be "
            f"checked against official data." if claims else
            f"No sentence on \u201c{name}\u201d ({page['host']}) states a figure that official data covers.")
    if got.get("unsettled"):
        head += f" {got['unsettled']} more with figures could not be settled from official data."
    if got["truncated"]:
        head += " The page holds more; only the first were checked."
    return {"text": "\n\n".join([head] + [_lines(c) for c in claims]),
            "structured": {"page": page, "claims": claims, "tally": {k: v for k, v in got["tally"].items() if v},
                           "truncated": got["truncated"]}}


async def _check_claim(arguments: Dict[str, Any], request: Request) -> Dict[str, Any]:
    claim = _text_arg(arguments, "claim", 1000)
    await _limited("verify", request)
    said = None
    if arguments.get("said_on"):
        from datetime import date
        try:
            said = date.fromisoformat(str(arguments["said_on"]))
        except ValueError:
            raise ToolError("said_on must be a date written YYYY-MM-DD.")
    got = await check_text(claim, client_ip=client_ip(request), free_only=True, said_on=said)
    # A sentence that holds several claims is answered claim by claim.
    claims = [_brief(e["result"]) for e in got["claims"]]
    if len(claims) == 1:
        return {"text": _lines(claims[0]), "structured": claims[0]}
    return {"text": "\n\n".join(_lines(c) for c in claims), "structured": {"claims": claims}}


async def _find_figure(arguments: Dict[str, Any], request: Request) -> Dict[str, Any]:
    query = _text_arg(arguments, "query", 200)
    await _limited("lookup", request)
    r = lookup_to_dict(await lookup(query, deep=False, client_ip=client_ip(request)))
    c = r.get("citations") or {}
    if not r.get("ok"):
        tries = [s.get("example") for s in (r.get("suggestions") or [])[:3] if s.get("example")]
        text = r.get("headline") or "No official series matches that search."
        if tries:
            text += " Try: " + "; ".join(tries) + "."
        return {"text": text, "structured": {"found": False, "message": r.get("headline"), "try": tries}}
    out = {"found": True, "label": r.get("label"), "figure": c.get("value") or r.get("value"),
           "attribution": c.get("attribution"), "caveat": c.get("caveat"), "source_url": c.get("url"),
           "citation": c.get("note")}
    out = {k: v for k, v in out.items() if v not in (None, "")}
    text = f"{out.get('label', 'Official figure')}: {out.get('figure', '')}"
    if out.get("attribution"):
        text += f" ({out['attribution']})"
    if out.get("caveat"):
        text += f"\nNote: {out['caveat']}"
    if out.get("citation"):
        text += f"\n{out['citation']}"
    return {"text": text, "structured": out}


_HANDLERS = {"check_text": _check_text, "check_page": _check_page, "check_claim": _check_claim,
             "find_figure": _find_figure}


# ---------------------------------------------------------------- protocol

def _ok(msg_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


async def _handle(msg: Any, request: Request) -> Optional[Dict[str, Any]]:
    """Answer one JSON-RPC message, or None for a notification."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid request.")
    method, msg_id, params = msg["method"], msg.get("id"), msg.get("params") or {}
    if "id" not in msg:
        return None  # a notification: nothing to answer

    if method == "initialize":
        asked = params.get("protocolVersion") if isinstance(params, dict) else None
        return _ok(msg_id, {
            "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        })
    if method == "ping":
        return _ok(msg_id, {})
    if method == "tools/list":
        return _ok(msg_id, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name") if isinstance(params, dict) else None
        handler = _HANDLERS.get(name)
        if handler is None:
            return _error(msg_id, -32602, f"Unknown tool: {name}")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _error(msg_id, -32602, "Tool arguments must be an object.")
        try:
            got = await handler(arguments, request)
        except ToolError as e:
            return _ok(msg_id, {"content": [{"type": "text", "text": str(e)}], "isError": True})
        except Exception:  # noqa: BLE001 — a tool must answer, not take the endpoint down
            logger.exception("MCP tool %s failed", name)
            return _ok(msg_id, {"content": [{"type": "text", "text": "The check could not be completed. "
                                             "This is not a judgment about the claim."}], "isError": True})
        return _ok(msg_id, {"content": [{"type": "text", "text": got["text"]}],
                            "structuredContent": got["structured"], "isError": False})
    return _error(msg_id, -32601, f"Method not found: {method}")


@router.post("/mcp", include_in_schema=False)
async def mcp(request: Request):
    if not config.MCP_ENABLED:
        raise HTTPException(status_code=404, detail="Not found")
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — not JSON
        return JSONResponse(_error(None, -32700, "Parse error."), status_code=400)

    if isinstance(body, list):  # a batch, which older protocol versions allow
        if not body or len(body) > 10:
            return JSONResponse(_error(None, -32600, "Invalid request."), status_code=400)
        answers = [a for a in [await _handle(m, request) for m in body] if a is not None]
        return JSONResponse(answers) if answers else Response(status_code=202)

    answer = await _handle(body, request)
    if answer is None:
        return Response(status_code=202)
    return JSONResponse(answer)


@router.get("/mcp", include_in_schema=False)
async def mcp_stream():
    """No server-to-client stream is offered: the protocol's way to say so."""
    if not config.MCP_ENABLED:
        raise HTTPException(status_code=404, detail="Not found")
    return Response(status_code=405, headers={"Allow": "POST"})
