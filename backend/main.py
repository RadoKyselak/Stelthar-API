import asyncio
import json
from typing import Dict, Any
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from config import check_api_keys_on_startup, logger
from models import VerifyRequest
from models.verdicts import VerificationResponse
from services.verification_service import VerificationService
from services.verify_v2 import to_response_dict, verify as verify_v2
from utils.validation import ValidationError
from middleware.context import RequestContextMiddleware, get_request_id
from exceptions import MiradorException, CircuitBreakerOpenException

app = FastAPI(
    title="Stelthar API",
    description="Government data-backed fact-checking API for Project Mirador",
    version="1.0.0"
)

@app.on_event("startup")
async def startup_event():
    check_api_keys_on_startup()
    logger.info("Stelthar API started successfully")

app.add_middleware(RequestContextMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

verification_service = VerificationService()

@app.exception_handler(ValidationError)
async def validation_exception_handler(request: Request, exc: ValidationError):
    request_id = get_request_id()
    logger.warning(
        f"Validation error",
        extra={
            "request_id": request_id,
            "error": str(exc),
            "path": request.url.path
        }
    )
    return JSONResponse(
        status_code=422,
        content={
            "detail": str(exc),
            "type": "validation_error",
            "request_id": request_id
        }
    )

@app.exception_handler(CircuitBreakerOpenException)
async def circuit_breaker_exception_handler(request: Request, exc: CircuitBreakerOpenException):
    request_id = get_request_id()
    logger.error(
        f"Circuit breaker open",
        extra={
            "request_id": request_id,
            "service": exc.details.get("service"),
            "failure_count": exc.details.get("failure_count")
        }
    )
    return JSONResponse(
        status_code=503,
        content={
            "detail": exc.message,
            "type": "service_unavailable",
            "request_id": request_id,
            "retry_after": 60
        },
        headers={"Retry-After": "60"}
    )

@app.exception_handler(MiradorException)
async def mirador_exception_handler(request: Request, exc: MiradorException):
    request_id = get_request_id()
    logger.error(
        f"Application error",
        extra={
            "request_id": request_id,
            "error_type": exc.__class__.__name__,
            "error": exc.message,
            "details": exc.details
        }
    )
    return JSONResponse(
        status_code=500,
        content={
            "detail": exc.message,
            "type": exc.__class__.__name__,
            "request_id": request_id,
            "details": exc.details
        }
    )

@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    request_id = get_request_id()
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": exc.detail,
            "request_id": request_id
        }
    )

@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    request_id = get_request_id()
    logger.exception(
        f"Unhandled exception",
        extra={
            "request_id": request_id,
            "error": str(exc),
            "path": request.url.path
        }
    )
    return JSONResponse(
        status_code=500,
        content={
            "detail": "An unexpected error occurred",
            "request_id": request_id
        }
    )

@app.get("/")
async def health_check():
    return {"status": "ok", "message": "Stelthar-API is running :)"}

@app.get("/health")
async def detailed_health_check():
    return {
        "status": "healthy",
        "service": "stelthar-api",
        "version": "1.0.0",
        "timestamp": asyncio.get_event_loop().time()
    }

@app.post("/v2/verify")
async def verify_v2_endpoint(req: VerifyRequest) -> Dict[str, Any]:
    """Evidence-first verification: exact number, stated period, direct link.

    Added alongside `/verify` rather than replacing it, so the extension already
    published to the Chrome Web Store keeps working while it migrates.

    Differences that matter to a caller:
      * `verdict` is Supported / Contradicted / Misleading / No verdict.
        "Inconclusive" is gone — a non-verdict always carries a `reason` naming
        what would have to change for an answer to exist, because a dead
        adapter and a genuinely unanswerable question are not the same problem.
      * `confidence` is a coarse tier, not a percentage. The old score's largest
        component had been returning a constant since its embedding model was
        retired, so the decimals were decoration.
      * `tier` and `model_calls` are reported, so the cost of an answer is
        visible rather than inferred. Most claims resolve at zero model calls.
      * `search_suggestions_html`, when present, MUST be rendered by any UI that
        shows the citations. That is a condition of using Google Search
        grounding, not a display preference.
    """
    claim = req.claim.strip()
    if not claim:
        raise HTTPException(status_code=400, detail="Claim cannot be empty.")

    result = await verify_v2(claim)
    logger.info(
        "v2 verification complete",
        extra={
            "request_id": get_request_id(),
            "tier": result.tier,
            "model_calls": result.model_calls,
            "verdict": result.verdict,
        },
    )
    return to_response_dict(result)


@app.post("/verify", response_model=VerificationResponse)
async def verify(req: VerifyRequest) -> VerificationResponse:
    claim = req.claim.strip()
    if not claim:
        raise HTTPException(status_code=400, detail="Claim cannot be empty.")
    
    request_id = get_request_id()
    logger.info(
        f"Processing verification request",
        extra={
            "request_id": request_id,
            "claim_length": len(claim)
        }
    )
    
    return await verification_service.verify_claim(claim)
