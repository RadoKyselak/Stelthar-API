"""GET /c?claim=...: one claim's check, as a page someone can be sent.

A result had no address: it could be copied as a citation but not passed on,
and a link to the site with the claim in it showed a link preview that said
nothing about what the check found.

This page carries the claim in its address, runs the check when it is opened
and puts the answer in the page and in its link preview. Two rules keep that
safe:

  * Free data only. A link can be opened by anyone, any number of times, and
    by every app that draws a preview of it. So the check here never reaches
    the paid tiers: a claim the free data settles shows its answer; any other
    shows the claim in the box, and the visitor presses Check, as on the home
    page.
  * The verdict in the preview is the one the check just reached. Nothing
    about the result is read from the address, so a link cannot be written to
    say "Supported" over a claim that is not.

The page is the home page with its head and one data block replaced (see
`page`), so there is one layout to keep.

The picture in the preview says the verdict too. It is one of five fixed
images, chosen by the verdict just reached: nothing is drawn per request, so
a preview costs no more than a file, and the claim's own words stay in the
preview's title, where the app that shows it sets them as text.
"""
import json
import pathlib
import re
from html import escape
from typing import Any, Dict, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from claims.verdict import NO_VERDICT
from config import logger
from services.presentation import share_url
from services.verify_v2 import to_response_dict, verify
from utils.request_limits import client_ip, limit
from utils.validation import InputValidator, ValidationError

router = APIRouter()

# Verdict key -> the picture for its link preview (backend/static, 1200x630).
PREVIEW_IMAGES = {
    "supported": "og-supported.png",
    "mostly-accurate": "og-mostly-accurate.png",
    "contradicted": "og-contradicted.png",
    "misleading": "og-misleading.png",
    "published": "og-published.png",       # matches a figure since revised
}

_INDEX = pathlib.Path(__file__).resolve().parent.parent / "static" / "index.html"
MAX_CLAIM = 600


def _cut(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rsplit(" ", 1)[0] + "…"


def _set_meta(html: str, attr: str, name: str, content: str) -> str:
    """Replace the content of <meta {attr}="{name}" content="...">. Raises if
    the tag is gone, so an edit to index.html cannot silently break previews."""
    pattern = re.compile(rf'(<meta {attr}="{re.escape(name)}" content=")[^"]*(")')
    if not pattern.search(html):
        raise LookupError(f"index.html has no <meta {attr}=\"{name}\">")
    return pattern.sub(lambda m: m.group(1) + escape(content, quote=True) + m.group(2), html, count=1)


def page(claim: str, result: Optional[Dict[str, Any]]) -> str:
    """The home page, retitled for this claim, with its answer inside."""
    html = _INDEX.read_text(encoding="utf-8")
    if result is not None:
        d = result["display"]
        title = f"{d['verdict']['label']}: “{_cut(claim, 110)}”"
        about = _cut(d.get("summary") or d["verdict"].get("meaning") or "", 200)
    else:
        title = f"Check: “{_cut(claim, 110)}”"
        about = "Check this claim against official U.S. government data, with the exact source."
    url = share_url(claim)

    if "<title>" not in html:
        raise LookupError("index.html has no <title>")
    html = re.sub(r"<title>[^<]*</title>", f"<title>{escape(title)} · Project Mirador</title>", html, count=1)
    tags = [
        ("name", "description", about),
        ("property", "og:title", title), ("property", "og:description", about), ("property", "og:url", url),
        ("name", "twitter:title", title), ("name", "twitter:description", about),
    ]
    if result is not None:
        d = result["display"]
        picture = PREVIEW_IMAGES.get("published" if d.get("revision") else d["verdict"]["key"])
        if picture:
            import config
            address = f"{config.PUBLIC_BASE_URL}/static/{picture}"
            tags += [("property", "og:image", address), ("name", "twitter:image", address),
                     ("property", "og:image:alt",
                      f"{d['verdict']['label']}: checked against official U.S. government data")]
    for attr, name, content in tags:
        html = _set_meta(html, attr, name, content)
    # Any claim can be put in this address: keep such pages out of search results.
    html = html.replace("</title>", '</title>\n  <meta name="robots" content="noindex">', 1)
    # The home page's canonical link would contradict that noindex.
    html = re.sub(r'\n *<link rel="canonical"[^>]*>', "", html, count=1)

    # The claim goes in the box; the answer in a data block the page's script
    # draws. "<" is escaped so no text in a result can close the block.
    box = re.compile(r'(<input id="q"[^>]*?)(\s*placeholder=)')
    if not box.search(html):
        raise LookupError("index.html has no claim box")
    html = box.sub(lambda m: f'{m.group(1)} value="{escape(claim, quote=True)}"{m.group(2)}', html, count=1)
    if result is not None:
        data = json.dumps(result).replace("<", "\\u003c")
        block = f'<script type="application/json" id="shared">{data}</script>\n  '
        marker = '<script src="/static/result-view.js"></script>'
        if marker not in html:
            raise LookupError("index.html no longer loads result-view.js")
        html = html.replace(marker, block + marker, 1)
    return html


@router.api_route("/c", methods=["GET", "HEAD"], dependencies=[Depends(limit("verify"))])
async def shared_check(request: Request, claim: str = ""):
    try:
        claim = InputValidator.sanitize_claim(claim)
    except ValidationError:
        return RedirectResponse("/", status_code=302)
    if len(claim) > MAX_CLAIM:
        return RedirectResponse("/", status_code=302)

    # allow_grounding=False: free government data only (see the module note).
    v = await verify(claim, allow_grounding=False, client_ip=client_ip(request))
    result = to_response_dict(v) if v.verdict != NO_VERDICT else None
    try:
        html = page(claim, result)
    except LookupError:
        logger.exception("The share page could not be built from index.html")
        return RedirectResponse(f"/?claim={quote(claim, safe='')}", status_code=302)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})
