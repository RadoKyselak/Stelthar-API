"""One address for search engines: factcheckgov.app.

The same deployment also answers on stelthar-api.vercel.app and on spare
domains. Pages fetched there by a browser or a crawler get a permanent
redirect to the same path on the canonical host, so search engines index one
copy instead of splitting it across addresses.

Only page requests move. The extension and API clients call
stelthar-api.vercel.app/v2/... directly; those requests, monitors asking "/"
for JSON, and preview deployments (any other host) are left alone.
"""
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Receive, Scope, Send

# Pages and the files crawlers read. /static/ (images, scripts) and the API stay put.
PAGE_PATHS = frozenset({
    "/", "/about", "/methodology", "/developers", "/privacy", "/c",
    "/sitemap.xml", "/llms.txt",
})


class CanonicalHostMiddleware:
    def __init__(self, app: ASGIApp, canonical_url: str, aliases, wants_page) -> None:
        self.app = app
        self.canonical = canonical_url.rstrip("/")
        self.canonical_host = urlsplit(self.canonical).hostname
        self.aliases = frozenset(h.lower() for h in aliases) - {self.canonical_host}
        self.wants_page = wants_page

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in ("GET", "HEAD") \
                or scope["path"] not in PAGE_PATHS:
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers") or []}
        host = headers.get("host", "").split(":")[0].lower()
        if host not in self.aliases or not self.wants_page(headers):
            await self.app(scope, receive, send)
            return
        target = self.canonical + scope["path"]
        query = scope.get("query_string", b"").decode("latin-1")
        if query:
            target += "?" + query
        await send({"type": "http.response.start", "status": 301,
                    "headers": [(b"location", target.encode("latin-1")),
                                (b"cache-control", b"public, max-age=3600"),
                                (b"content-length", b"0")]})
        await send({"type": "http.response.body", "body": b""})
