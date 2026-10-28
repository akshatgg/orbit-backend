"""Safe, polite HTTP for the importers (port of `fetch.ts` and `url-guard.ts`).

Every feed or page request is checked against private networks (on every redirect hop,
after DNS resolution), capped at 15 s and 5 MB, and sent with Orbit's User-Agent.
Platform HTTP errors surface as `FetchHttpError` so callers can react to 403 / 429 / 503.
"""

import asyncio
import ipaddress
import json
import re
import socket
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import settings

from . import js, urls

USER_AGENT = "ORBIT event sync (+https://orbit.akshatgg.in)"
TIMEOUT_SECONDS = 15.0
MAX_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 3
DEFAULT_ACCEPT = "text/calendar, text/plain;q=0.8, */*;q=0.1"


class FetchHttpError(Exception):
    """An HTTP error status from the remote server."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


# ─── SSRF guard ───────────────────────────────────────────────────────────────

_PRIVATE_V4 = [
    ipaddress.ip_network(cidr)
    for cidr in (
        "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
        "172.16.0.0/12", "192.0.0.0/24", "192.168.0.0/16", "198.18.0.0/15", "224.0.0.0/3",
    )
]
_PRIVATE_HOSTNAMES = re.compile(r"(^|\.)(localhost|local|internal|localdomain|home\.arpa)$", re.I | re.A)


def _ip_version(value: str) -> int:
    try:
        return ipaddress.ip_address(value).version
    except ValueError:
        return 0


def is_loopback(ip: str) -> bool:
    if _ip_version(ip) == 4:
        return ipaddress.ip_address(ip) in _PRIVATE_V4[3]
    v6 = ip.lower()
    return v6 in ("::1", "0:0:0:0:0:0:0:1") or (v6.startswith("::ffff:") and is_loopback(v6[7:]))


def is_private_address(ip: str) -> bool:
    """Private, loopback, link-local, CGNAT, multicast and other non-public addresses."""
    version = _ip_version(ip)
    if version == 4:
        address = ipaddress.ip_address(ip)
        return any(address in network for network in _PRIVATE_V4)
    if version == 6:
        v6 = ip.lower()
        if v6.startswith("::ffff:"):
            mapped = v6[7:]
            return is_private_address(mapped) if _ip_version(mapped) == 4 else True
        return (
            v6 in ("::", "::1") or v6.startswith(("fc", "fd", "ff")) or bool(re.match(r"^fe[89ab]", v6))
        )
    return True  # not an IP at all → treat as unsafe


@dataclass
class UrlCheck:
    ok: bool
    url: Any = None
    error: str | None = None


def _allow_loopback() -> bool:
    return settings.is_local


def check_feed_url(raw: str, allow_loopback: bool | None = None) -> UrlCheck:
    """Static checks on a feed URL (protocol, credentials, obvious private hosts)."""
    allow = _allow_loopback() if allow_loopback is None else allow_loopback
    url = urls.parse(js.trim(raw) if isinstance(raw, str) else "")
    if url is None:
        return UrlCheck(False, error="Enter a full URL starting with https://")
    if url.scheme == "webcal":
        url = urls.parse(f"https:{url.href[len('webcal:'):]}")
        if url is None:
            return UrlCheck(False, error="Enter a full URL starting with https://")
    if url.scheme not in ("http", "https"):
        return UrlCheck(False, error="Only http(s) feed URLs are allowed.")
    if url.username or url.password:
        return UrlCheck(False, error="Feed URLs can't contain a username or password.")

    host = re.sub(r"^\[|\]$", "", url.hostname)
    loopback_name = host == "localhost" or host.endswith(".localhost")
    if _ip_version(host):
        if is_private_address(host) and not (allow and is_loopback(host)):
            return UrlCheck(False, error="Feeds on private or local network addresses aren't allowed.")
    elif loopback_name:
        if not allow:
            return UrlCheck(False, error="Feeds on localhost aren't allowed.")
    elif _PRIVATE_HOSTNAMES.search(host) or "." not in host:
        return UrlCheck(False, error="Use a public feed URL.")
    return UrlCheck(True, url=url)


def check_resolved_addresses(addresses: list[str], allow_loopback: bool | None = None) -> str | None:
    allow = _allow_loopback() if allow_loopback is None else allow_loopback
    if not addresses:
        return "The feed's host didn't resolve."
    bad = next((a for a in addresses if is_private_address(a) and not (allow and is_loopback(a))), None)
    return "The feed's host points at a private network address." if bad else None


async def _resolve(host: str) -> list[str]:
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise Exception(f"getaddrinfo ENOTFOUND {host}") from exc
    return list(dict.fromkeys(str(info[4][0]).split("%")[0] for info in infos))


async def _assert_safe_url(raw: str) -> urls.URL:
    check = check_feed_url(raw)
    if not check.ok:
        raise Exception(check.error)
    host = re.sub(r"^\[|\]$", "", check.url.hostname)
    addresses = [host] if _ip_version(host) else await _resolve(host)
    problem = check_resolved_addresses(addresses)
    if problem:
        raise Exception(problem)
    return check.url


def _network_code(exc: BaseException) -> str | None:
    cause: BaseException | None = exc
    while cause is not None:
        if isinstance(cause, socket.gaierror):
            return "ENOTFOUND"
        if isinstance(cause, ConnectionRefusedError):
            return "ECONNREFUSED"
        if isinstance(cause, ConnectionResetError):
            return "ECONNRESET"
        cause = cause.__cause__ or cause.__context__
    return None


def _decode(body: bytes) -> str:
    # TextDecoder("utf-8"): always UTF-8, a leading BOM dropped, bad bytes replaced.
    return body.decode("utf-8-sig", errors="replace")


async def fetch_feed_text(raw_url: str, accept: str = DEFAULT_ACCEPT) -> str:
    """Fetches a feed or page politely and safely; returns its text."""
    url = await _assert_safe_url(raw_url)
    # The headers Node's fetch sends, so platforms answer as they did the TypeScript engine.
    headers = {"User-Agent": USER_AGENT, "Accept": accept, "Accept-Language": "*", "Sec-Fetch-Mode": "cors"}
    try:
        async with asyncio.timeout(TIMEOUT_SECONDS):
            async with httpx.AsyncClient(follow_redirects=False, timeout=None) as client:
                hop = 0
                while True:
                    async with client.stream("GET", url.href, headers=headers) as response:
                        location = response.headers.get("location")
                        if 300 <= response.status_code < 400 and location:
                            if hop == MAX_REDIRECTS:
                                raise Exception("The feed redirected too many times.")
                            next_url = _join(url.href, location)
                            url = await _assert_safe_url(next_url)
                            hop += 1
                            continue
                        status = response.status_code
                        if status in (429, 503):
                            raise FetchHttpError(
                                f"The feed asked us to slow down (HTTP {status}). We'll try again on the next refresh.",
                                status,
                            )
                        if not 200 <= status < 300:
                            reason = f" {response.reason_phrase}" if response.reason_phrase else ""
                            raise FetchHttpError(f"The feed returned HTTP {status}{reason}.", status)
                        declared = response.headers.get("content-length")
                        if declared and declared.isdigit() and int(declared) > MAX_BYTES:
                            raise Exception("The feed is larger than 5 MB.")
                        chunks: list[bytes] = []
                        total = 0
                        async for chunk in response.aiter_bytes():
                            total += len(chunk)
                            if total > MAX_BYTES:
                                raise Exception("The feed is larger than 5 MB.")
                            chunks.append(chunk)
                        return _decode(b"".join(chunks))
    except TimeoutError as exc:
        raise Exception("The feed took too long to respond (15s).") from exc
    except httpx.TransportError as exc:
        code = _network_code(exc)
        raise Exception(f"Couldn't reach the feed{f' ({code})' if code else ''}.") from exc


def _join(base: str, location: str) -> str:
    """`new URL(location, base).toString()` for redirect targets."""
    absolute = urls.parse(location)
    if absolute is not None:
        return absolute.href
    resolved = str(httpx.URL(base).join(location))
    parsed = urls.parse(resolved)
    return parsed.href if parsed else resolved


async def post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float = 20.0) -> tuple[int, Any]:
    """A plain JSON POST (Meetup's GraphQL): the status and the parsed body (None if not JSON).

    Like the TypeScript `fetch` it replaces, this skips the feed guard: the URL is a fixed
    platform endpoint, never one an admin typed.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.post(url, content=json.dumps(payload, separators=(",", ":")), headers=headers)
    except httpx.TimeoutException as exc:
        raise Exception("The operation was aborted due to timeout") from exc
    except httpx.TransportError as exc:
        raise Exception("fetch failed") from exc
    try:
        body = response.json()
    except ValueError:
        body = None
    return response.status_code, body


async def sleep(ms: int) -> None:
    await asyncio.sleep(ms / 1000)
