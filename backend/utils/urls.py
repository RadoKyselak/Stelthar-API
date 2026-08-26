"""Build citation URLs that carry only data-identifying parameters.

Several data sources authenticate by query parameter (BEA `UserID`, Census
`key`, Congress.gov `api_key`). httpx merges those into the request URL, so
`str(response.url)` is a fully-formed URL mixing auth and transport parameters
in with the ones that actually identify the data.

Citation URLs are user-facing: they are returned in API responses, rendered as
link text by the extension, and included in LLM prompt context. A citation
should carry exactly what lets a reader reproduce the lookup — table, year,
line code — and nothing else.

Redaction lives here rather than in each adapter so that it is a single choke
point every URL-capture site routes through, and a new adapter is covered by
default rather than by remembering.

The function FAILS CLOSED. If the URL cannot be parsed, or anything else goes
wrong, it returns the origin and path with the entire query string dropped
rather than returning the input unchanged. A citation missing some query
parameters is a cosmetic problem; the alternative is not, and a guard that
quietly passes through input it does not understand is not a guard.
"""
from typing import Any, Iterable, Set
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Matched case-insensitively against query parameter NAMES. Deliberately broad:
# a false positive costs a dropped query parameter, a false negative leaks a
# credential.
_SECRET_PARAM_NAMES: Set[str] = {
    "userid",           # BEA
    "key",              # Census
    "api_key", "apikey", "api-key", "x-api-key",
    "registrationkey",  # BLS (normally POST body, but be safe)
    "subscription-key", "subscription_key",
    "token", "access_token", "auth_token", "id_token", "refresh_token",
    "auth", "authorization", "password", "passwd", "pwd", "secret",
    "client_secret", "signature", "sig",
}


def is_secret_param(name: str) -> bool:
    """True if a query parameter name looks like it carries a credential."""
    n = (name or "").strip().lower()
    if not n:
        return False
    if n in _SECRET_PARAM_NAMES:
        return True
    # Catch vendor-prefixed variants such as "bea_userid" or "x_api_key".
    # "userid" is matched bare rather than only with a separator, so both
    # "bea_userid" and "beauserid" are covered.
    return any(
        n.endswith(suffix)
        for suffix in (
            "_key", "-key", "_token", "-token", "_secret", "-secret",
            "userid", "_password", "-password", "_pwd", "-pwd",
        )
    )


def public_url(url: Any, extra_drop: Iterable[str] = ()) -> str:
    """Return `url` with every credential-bearing query parameter removed.

    Accepts a str or an httpx.URL. Non-secret parameters are preserved, so the
    citation still shows which table, year and line code produced the number —
    the provenance that makes a verdict checkable.

    >>> public_url("https://apps.bea.gov/api/data?UserID=SECRET&TableName=T31600")
    'https://apps.bea.gov/api/data?TableName=T31600'
    >>> public_url("https://api.census.gov/data/2023/acs/acs1?get=NAME&key=SECRET")
    'https://api.census.gov/data/2023/acs/acs1?get=NAME'
    """
    raw = str(url or "")
    if not raw:
        return ""

    drop = {str(d).strip().lower() for d in extra_drop}

    try:
        parts = urlsplit(raw)
        kept = [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not is_secret_param(k) and k.strip().lower() not in drop
        ]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))
    except Exception:
        # Fail closed: drop the whole query rather than risk echoing a secret.
        try:
            parts = urlsplit(raw)
            return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        except Exception:
            return ""
