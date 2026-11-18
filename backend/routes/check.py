"""POST /v2/check: every claim in a passage, each checked on its own."""
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request

import config
from config import logger
from middleware.context import get_request_id
from models import CheckRequest
from services.check_link import check_link
from services.check_text import check_text
from utils.safe_fetch import FetchFailed, FetchRefused
from utils.request_limits import client_ip, limit

router = APIRouter()


@router.post("/v2/check", dependencies=[Depends(limit("check"))])
async def check_endpoint(req: CheckRequest, request: Request) -> Dict[str, Any]:
    """A paragraph, a speech or the figures on a page, checked claim by claim.

    Answers with `claims` (in the order they appear, each with the same
    `result` `/v2/verify` returns) and a `tally` of how they fell.

      * One claim in the text gets the full pipeline, as `/v2/verify` does.
      * Two or more are checked on free government data only. One the free
        data cannot settle comes back "Can't verify" with `deeper: true`; send
        that claim to `/v2/verify` for the full check.
      * `mode: "page"` keeps only claims the free data can settle, and never
        spends a model call.
      * `url` instead of `text` fetches that page and checks it as a page. The
        answer carries `page` (its address, site and title). An address that
        cannot be read answers 400 with the reason.
      * `said_on` (YYYY-MM-DD) is the day the text was said. A claim in it
        that names no period is held to the figure that was newest then.
      * `span` is the claim exactly as written; `claim` is what was checked,
        which adds a year or place the sentence stated once for several
        claims. `inherited` says what was added.
    """
    if req.url is not None:
        if not config.CHECK_LINKS:
            raise HTTPException(status_code=400, detail="Checking a page by its address is switched off. "
                                                        "Paste the text instead.")
        try:
            result = await check_link(req.url, client_ip=client_ip(request))
        except (FetchRefused, FetchFailed) as e:
            # Both are for the person who asked: what was wrong, and what works.
            raise HTTPException(status_code=400, detail=str(e))
    else:
        from datetime import date
        said = date.fromisoformat(req.said_on) if req.said_on else None
        result = await check_text(req.text, mode=req.mode, client_ip=client_ip(request), said_on=said)
        result["said_on"] = req.said_on or ""
    from utils import usage
    await usage.count("page" if req.url is not None else "passage")
    await usage.count("claims", result["count"])
    await usage.count_verdicts(e["result"]["display"]["verdict"]["key"] for e in result["claims"])
    logger.info(
        "passage check complete",
        extra={
            "request_id": get_request_id(),
            "mode": result["mode"],
            "claims": result["count"],
            "model_calls": result["model_calls"],
        },
    )
    return result
