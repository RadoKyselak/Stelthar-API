"""Protections every response gets, before any route runs.

The API is public and unauthenticated (the extension and the web page call
it from people's browsers), so the defences are about what a stranger can
make it do:

  * Security headers on every response. The web page may load only its own
    files and talk only to this API (no inline script or style, no framing),
    so injected markup cannot run and the page cannot be framed for
    clickjacking. API responses forbid everything: they are data, never a page.
  * A size cap on request bodies. A claim is at most 1,000 characters; a
    multi-megabyte body is never a claim.

Per-visitor request limits live in utils.request_limits, and the paid AI
tier keeps its own spend limits in utils.spend_guard.
"""
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# The web page: its own scripts, styles and images, its own API, nothing else.
PAGE_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; "
    "object-src 'none'; upgrade-insecure-requests"
)
# Everything else is data: nothing in it may load, run or be framed.
DATA_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"

_COMMON = {
    b"x-content-type-options": b"nosniff",
    b"x-frame-options": b"DENY",
    b"referrer-policy": b"no-referrer",
    b"strict-transport-security": b"max-age=63072000; includeSubDomains",
    b"permissions-policy": (b"camera=(), microphone=(), geolocation=(), payment=(), usb=(), "
                            b"browsing-topics=()"),
    b"cross-origin-opener-policy": b"same-origin",
}

MAX_BODY_BYTES = 16 * 1024


class SecurityMiddleware:
    def __init__(self, app: ASGIApp, max_body: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_body = max_body

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        length = headers.get(b"content-length")
        if length is not None:
            try:
                too_big = int(length) > self.max_body
            except ValueError:
                too_big = True
            if too_big:
                await _plain(send, 413, b'{"detail":"Request body too large."}')
                return

        # Count what actually arrives too: a body sent without a length (or
        # with a false one) is cut off at the cap.
        received = 0

        async def capped_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body:
                    raise _BodyTooLarge()
            return message

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                out = list(message.get("headers") or [])
                present = {k.lower() for k, _ in out}
                ctype = next((v for k, v in out if k.lower() == b"content-type"), b"")
                page = ctype.startswith(b"text/html")
                extra = dict(_COMMON)
                extra[b"content-security-policy"] = (PAGE_CSP if page else DATA_CSP).encode()
                if page or scope.get("path", "").startswith("/static/"):
                    extra[b"cross-origin-resource-policy"] = b"same-origin"
                if not page and scope.get("method") == "POST":
                    extra[b"cache-control"] = b"no-store"
                out += [(k, v) for k, v in extra.items() if k not in present]
                message = {**message, "headers": out}
            await send(message)

        try:
            await self.app(scope, capped_receive, with_headers)
        except _BodyTooLarge:
            await _plain(send, 413, b'{"detail":"Request body too large."}')


class _BodyTooLarge(Exception):
    pass


async def _plain(send: Send, status: int, body: bytes) -> None:
    hdrs = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
            (b"content-security-policy", DATA_CSP.encode())]
    hdrs += list(_COMMON.items())
    await send({"type": "http.response.start", "status": status, "headers": hdrs})
    await send({"type": "http.response.body", "body": body})
