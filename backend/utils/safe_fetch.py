"""Download one public web page that a visitor named, and nothing else.

Checking an article by its address means this server fetches an address a
stranger typed. Left unguarded, that is a way to make the server read things
only it can reach: a cloud provider's metadata service, another machine on a
private network, a service on localhost. So the rules here are about where a
request may go, and they are checked again at every redirect:

  * http and https only, on their standard ports, with no user name in the
    address and no bare IP address as the host.
  * The host must resolve only to public addresses. One private, loopback,
    link-local or otherwise reserved address among them refuses the request.
  * The connection is made to the address that was checked, not to the name
    (which could resolve to something else a moment later). The name is still
    sent as the Host header and used to verify the TLS certificate.
  * A page is at most 2 MB of HTML or plain text, fetched within a few
    seconds, through at most four redirects.

`fetch_public_page` raises `FetchRefused` for an address it will not fetch
(the caller's fault, say why) and `FetchFailed` when the page could not be had
(not a judgment about the page).
"""
import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

import httpx

from config import logger

MAX_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 4
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
# Says who is asking and where to read about it; sites can then choose.
USER_AGENT = "Mozilla/5.0 (compatible; ProjectMirador/2.2; +https://stelthar-api.vercel.app/about)"
_PORTS = {"http": 80, "https": 443}
_TEXT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")


class FetchRefused(Exception):
    """The address is one this server will not fetch."""


class FetchFailed(Exception):
    """The page could not be downloaded or is not readable text."""


@dataclass
class Page:
    url: str            # where the content came from, after redirects
    host: str
    content_type: str
    text: str


def _is_public(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return addr.is_global and not (addr.is_multicast or addr.is_reserved)


def check_address(url: str) -> Tuple[str, str, int, str]:
    """(scheme, host, port, path-and-query) of an address that may be fetched,
    judged by its text alone. Raises FetchRefused."""
    try:
        parts = urlsplit((url or "").strip())
        port = parts.port
    except ValueError:
        raise FetchRefused("That is not a web address.")
    scheme = (parts.scheme or "").lower()
    if scheme not in _PORTS:
        raise FetchRefused("Only http and https addresses can be checked.")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host or parts.username or parts.password:
        raise FetchRefused("That is not a web address.")
    if port not in (None, _PORTS[scheme]):
        raise FetchRefused("Only pages on the standard web ports can be checked.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise FetchRefused("Give the page's name, not a numeric address.")
    if "." not in host or host.endswith((".local", ".internal", ".localhost", ".lan", ".home", ".corp")):
        raise FetchRefused("That address is not on the public web.")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise FetchRefused("That is not a web address.")
    path = parts.path or "/"
    if parts.query:
        path += f"?{parts.query}"
    return scheme, host, _PORTS[scheme], path


async def _resolve(host: str, port: int) -> List[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError):
        raise FetchFailed("That site could not be found.")
    ips = []
    for info in infos:
        ip = info[4][0]
        if ip not in ips:
            ips.append(ip)
    if not ips:
        raise FetchFailed("That site could not be found.")
    if not all(_is_public(ip) for ip in ips):
        raise FetchRefused("That address is not on the public web.")
    return ips


def _literal(ip: str) -> str:
    return f"[{ip}]" if ":" in ip else ip


async def fetch_public_page(url: str, client: Optional[httpx.AsyncClient] = None) -> Page:
    own = client is None
    # trust_env=False: no proxy or other setting from the environment can
    # send the request somewhere the checks above did not look at.
    client = client or httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False, trust_env=False)
    try:
        for _ in range(MAX_REDIRECTS + 1):
            scheme, host, port, path = check_address(url)
            ips = await _resolve(host, port)
            # IPv4 first: serverless hosts often have no IPv6 route.
            ip = next((i for i in ips if ":" not in i), ips[0])
            request = client.build_request(
                "GET", f"{scheme}://{_literal(ip)}{path}",
                headers={"Host": host, "User-Agent": USER_AGENT,
                         "Accept": "text/html,application/xhtml+xml;q=0.9,text/plain;q=0.8",
                         "Accept-Language": "en"},
                extensions={"sni_hostname": host},
            )
            try:
                response = await client.send(request, stream=True)
            except httpx.HTTPError as e:
                logger.info("Page fetch failed for %s: %s", host, type(e).__name__)
                raise FetchFailed("That page did not respond.")
            try:
                if response.status_code in (301, 302, 303, 307, 308):
                    where = response.headers.get("location")
                    if not where:
                        raise FetchFailed("That page did not respond.")
                    url = urljoin(f"{scheme}://{host}{path}", where)
                    continue
                if response.status_code in (401, 402, 403, 451):
                    raise FetchFailed("That site does not let its pages be read this way.")
                if response.status_code >= 400:
                    raise FetchFailed(f"That page could not be opened (the site answered {response.status_code}).")
                ctype = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if ctype and ctype not in _TEXT_TYPES:
                    raise FetchFailed("That address is not a page of text.")
                chunks, total = [], 0
                async for chunk in response.aiter_bytes():
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= MAX_BYTES:
                        break
                data = b"".join(chunks)[:MAX_BYTES]
                encoding = response.charset_encoding or "utf-8"
                try:
                    text = data.decode(encoding, errors="replace")
                except LookupError:
                    text = data.decode("utf-8", errors="replace")
                return Page(url=f"{scheme}://{host}{path}", host=host, content_type=ctype or "text/html", text=text)
            except httpx.HTTPError as e:
                logger.info("Page read failed for %s: %s", host, type(e).__name__)
                raise FetchFailed("That page did not respond.")
            finally:
                await response.aclose()
        raise FetchFailed("That address redirects too many times.")
    finally:
        if own:
            await client.aclose()
