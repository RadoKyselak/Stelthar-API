"""POST /v2/check: every claim in a passage, each checked on its own."""
from typing import Any, Dict

from fastapi import APIRouter, Depends, Request

from config import logger
from middleware.context import get_request_id
from models import CheckRequest
from services.check_text import check_text
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
      * `mode: "page"` (the extension's page scan) keeps only claims the free
        data can settle, and never spends a model call.
      * `span` is the claim exactly as written; `claim` is what was checked,
        which adds a year or place the sentence stated once for several
        claims. `inherited` says what was added.
    """
    result = await check_text(req.text, mode=req.mode, client_ip=client_ip(request))
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
