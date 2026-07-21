import re
import uuid
import time
from contextvars import ContextVar
from typing import Optional
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from config import logger

request_id_var: ContextVar[Optional[str]] = ContextVar("request_id", default=None)
request_start_time_var: ContextVar[Optional[float]] = ContextVar("request_start_time", default=None)

# Client-supplied request IDs are logged and echoed back in a response header.
# Restrict to a safe charset/length so a malicious value can't inject fake log
# lines or oversized header content; well-behaved clients doing distributed
# tracing still get their ID correlated.
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _resolve_request_id(request: Request) -> str:
    client_id = request.headers.get("X-Request-ID")
    if client_id and _SAFE_REQUEST_ID.match(client_id):
        return client_id
    return str(uuid.uuid4())


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = _resolve_request_id(request)
        request_id_var.set(request_id)
        
        start_time = time.time()
        request_start_time_var.set(start_time)
        
        logger.info(
            f"Request started",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "client": request.client.host if request.client else None
            }
        )
        
        response = await call_next(request)
        
        duration = time.time() - start_time
        
        logger.info(
            f"Request completed",
            extra={
                "request_id": request_id,
                "status_code": response.status_code,
                "duration_ms": round(duration * 1000, 2)
            }
        )
        
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time"] = f"{duration:.3f}s"
        
        return response

def get_request_id() -> Optional[str]:
    return request_id_var.get()

def get_request_duration() -> Optional[float]:
    start_time = request_start_time_var.get()
    if start_time:
        return time.time() - start_time
    return None
