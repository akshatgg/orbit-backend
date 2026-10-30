"""Links in messages: finding them, and fetching previews of outside pages.

A URL in a message's text becomes a `message_links` row in the same transaction; deleted
messages lose theirs. The preview is fetched after the commit, in the background, and never
holds a database connection while it waits on someone else's server.

Anyone can make the server fetch a URL by sending it, so the fetch trusts nothing: http(s)
only, default ports only, every address the host resolves to must be public, the connection
goes to the address that was checked (a host can't answer differently the second time), and
each redirect is checked the same way. 5 seconds, 1 MB, HTML only. It never raises.

Links to this site aren't fetched: an event's preview comes from the event itself.
"""

import asyncio
import html
import ipaddress
import json
import logging
import re
from functools import partial
from html.parser import HTMLParser
from typing import Any, Optional
from urllib.parse import urljoin, urlsplit

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.fetch import _resolve, check_feed_url, check_resolved_addresses, is_private_address
from app.core.config import settings
from app.notifications.service import _after_commit

logger = logging.getLogger(__name__)

MAX_LINKS_PER_MESSAGE = 10
MAX_URL_LENGTH = 2000
TIMEOUT_SECONDS = 5.0
MAX_BYTES = 1024 * 1024
MAX_REDIRECTS = 3
PREVIEWS_AT_ONCE = 4
USER_AGENT = "ORBIT link preview (+https://orbit.akshatgg.in)"
DEFAULT_PORTS = {"http": 80, "https": 443}

_URL = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)
_TRAILING = ".,;:!?*_~"
_CLOSERS = {")": "(", "]": "[", "}": "{"}
_OWN_EVENT = re.compile(r"/events/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/?\Z", re.I)

_semaphore = asyncio.Semaphore(PREVIEWS_AT_ONCE)


class Refused(Exception):
    """A URL the preview fetch won't touch."""


# ─── finding links ──────────────────────────────────────────────────────────


def _trim(url: str) -> str:
    """Drop what a sentence put after a URL: punctuation, and a closing bracket it didn't open."""
    while url:
        last = url[-1]
        if last in _TRAILING:
            url = url[:-1]
        elif last in _CLOSERS and url.count(last) > url.count(_CLOSERS[last]):
            url = url[:-1]
        else:
            break
    return url


def domain_of(url: str) -> Optional[str]:
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


def extract_urls(body: str) -> list[str]:
    """The http(s) URLs in a message's text, in order, without repeats, at most 10."""
    found: list[str] = []
    for match in _URL.finditer(body or ""):
        url = _trim(match.group(0))
        if url not in found and len(url) <= MAX_URL_LENGTH and domain_of(url):
            found.append(url)
        if len(found) == MAX_LINKS_PER_MESSAGE:
            break
    return found


def _own_site(url: str) -> bool:
    return domain_of(url) == domain_of(settings.FRONTEND_URL)


def _own_event_id(url: str) -> Optional[str]:
    if not _own_site(url):
        return None
    match = _OWN_EVENT.fullmatch(urlsplit(url).path)
    return match.group(1).lower() if match else None


async def sync(db: AsyncSession, message_id: str, conversation_id: str, body: str) -> None:
    """Make a message's link rows match its text (on send and edit), and queue the previews that
    are missing. A fixed number of statements."""
    urls = extract_urls(body)
    await db.execute(
        text("delete from public.message_links where message_id = :m and url <> all(cast(:urls as text[]))"),
        {"m": message_id, "urls": urls},
    )
    if not urls:
        return
    event_ids = {url: _own_event_id(url) for url in urls}
    wanted = [e for e in event_ids.values() if e]
    events: dict[str, Any] = {}
    if wanted:
        rows = (
            await db.execute(
                text(
                    "select e.id, e.title, e.summary, e.cover_url from public.events e"
                    " left join public.organisers o on o.id = e.organiser_id"
                    " where e.id = any(cast(:ids as uuid[])) and e.status = 'published'"
                    " and (e.organiser_id is null or o.status = 'approved')"
                ),
                {"ids": wanted},
            )
        ).mappings().all()
        events = {str(r["id"]): r for r in rows}
    payload = []
    for url in urls:
        event = events.get(event_ids[url] or "")
        payload.append(
            {
                "url": url,
                "domain": domain_of(url),
                "title": event["title"] if event else None,
                "description": event["summary"] if event else None,
                "image_url": event["cover_url"] if event else None,
                "own": _own_site(url),
            }
        )
    inserted = (
        await db.execute(
            text(
                """
                insert into public.message_links
                  (message_id, conversation_id, url, domain, title, description, image_url, fetched_at)
                select :m, :c, x.url, x.domain, x.title, x.description, x.image_url,
                       case when x.own then now() end
                  from jsonb_to_recordset(cast(:rows as jsonb)) as x(
                    url text, domain text, title text, description text, image_url text, own boolean
                  )
                on conflict (message_id, url) do nothing
                returning id, url, fetched_at
                """
            ),
            {"m": message_id, "c": conversation_id, "rows": json.dumps(payload)},
        )
    ).mappings().all()
    outside = [(str(r["id"]), r["url"]) for r in inserted if r["fetched_at"] is None]
    if outside:
        try:
            _after_commit(db, f"link previews {message_id}", partial(fill_previews, links=outside))
        except Exception:
            logger.exception("[links] previews not queued")


# ─── previews ───────────────────────────────────────────────────────────────


def is_public_address(ip: str) -> bool:
    """A globally routable address. Stricter than the importers' `is_private_address`: also
    refuses IPv4 tunnelled in IPv6 (any spelling), NAT64, and anything Python calls not global."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if address.version == 6:
        if address.ipv4_mapped or address.sixtofour or address.teredo:
            return False
        if address in ipaddress.ip_network("64:ff9b::/96"):
            return False
    return address.is_global and not address.is_multicast and not is_private_address(str(address))


async def safe_target(raw: str) -> tuple[httpx.URL, str]:
    """The URL and the one public address to connect to. `Refused` for anything else."""
    check = check_feed_url(raw, allow_loopback=False)
    if not check.ok:
        raise Refused(check.error)
    url = httpx.URL(check.url.href)
    if url.scheme not in DEFAULT_PORTS or (url.port or DEFAULT_PORTS[url.scheme]) != DEFAULT_PORTS[url.scheme]:
        raise Refused("Only http(s) on the default ports")
    host = url.host
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        addresses = await _resolve(host)
    problem = check_resolved_addresses(addresses, allow_loopback=False)
    if problem:
        raise Refused(problem)
    bad = next((a for a in addresses if not is_public_address(a)), None)
    if bad:
        raise Refused(f"{host} points at a non-public address")
    # IPv4 first: a server without IPv6 would otherwise time out on an AAAA answer.
    return url, sorted(addresses, key=lambda a: ":" in a)[0]


async def _fetch_html(raw: str) -> Optional[str]:
    """The page's HTML, following up to 3 checked redirects. None when it isn't an HTML page.
    `Refused` when the guard stops it; network errors raise httpx's."""
    current = raw
    async with httpx.AsyncClient(follow_redirects=False, timeout=TIMEOUT_SECONDS, trust_env=False) as client:
        for hop in range(MAX_REDIRECTS + 1):
            url, address = await safe_target(current)
            pinned = url.copy_with(host=address)
            request = client.build_request(
                "GET",
                pinned,
                headers={
                    "Host": url.netloc.decode("ascii"),
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml;q=0.9",
                },
                extensions={"sni_hostname": url.host},
            )
            response = await client.send(request, stream=True)
            try:
                location = response.headers.get("location")
                if 300 <= response.status_code < 400 and location:
                    if hop == MAX_REDIRECTS:
                        raise Refused("Too many redirects")
                    current = urljoin(str(url), location)
                    continue
                if not 200 <= response.status_code < 300:
                    return None
                kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if kind not in ("text/html", "application/xhtml+xml"):
                    return None
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= MAX_BYTES:
                        break
                return b"".join(chunks)[:MAX_BYTES].decode(response.encoding or "utf-8", errors="replace")
            finally:
                await response.aclose()
    return None


class _HeadParser(HTMLParser):
    """Open Graph tags, the description and the title, up to the end of <head>."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title = ""
        self._in_title = False
        self.done = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag == "title":
            self._in_title = True
        elif tag == "body":
            self.done = True
        elif tag == "meta":
            found = dict(attrs)
            key = (found.get("property") or found.get("name") or "").lower()
            content = found.get("content")
            if key and content and key not in self.meta:
                self.meta[key] = content

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag == "head":
            self.done = True

    def handle_data(self, data: str) -> None:
        if self._in_title and not self.done:
            self.title += data


def _tidy(value: Optional[str], limit: int) -> Optional[str]:
    if not value:
        return None
    return re.sub(r"\s+", " ", html.unescape(value)).strip()[:limit] or None


def parse_preview(page: str, base_url: str) -> dict[str, Optional[str]]:
    parser = _HeadParser()
    try:
        parser.feed(page)
    except Exception:  # noqa: BLE001 - a broken page has no preview, nothing more
        logger.debug("[links] couldn't parse %s", base_url)
    meta = parser.meta
    image = meta.get("og:image") or meta.get("og:image:url") or meta.get("twitter:image")
    image_url = urljoin(base_url, image.strip()) if image else None
    if image_url and (urlsplit(image_url).scheme not in DEFAULT_PORTS or len(image_url) > MAX_URL_LENGTH):
        image_url = None
    return {
        "title": _tidy(meta.get("og:title") or meta.get("twitter:title") or parser.title, 300),
        "description": _tidy(
            meta.get("og:description") or meta.get("twitter:description") or meta.get("description"), 500
        ),
        "image_url": image_url,
    }


async def fetch_preview(url: str) -> Optional[dict[str, Optional[str]]]:
    """A page's title, description and image, or None. Never raises."""
    try:
        async with _semaphore, asyncio.timeout(TIMEOUT_SECONDS):
            page = await _fetch_html(url)
        return parse_preview(page, url) if page else None
    except Refused as exc:
        logger.info("[links] refused %s: %s", url, exc)
    except Exception as exc:  # noqa: BLE001 - someone else's server; nothing to do about it
        logger.info("[links] no preview for %s: %s", url, type(exc).__name__)
    return None


async def fill_previews(db: AsyncSession, links: list[tuple[str, str]]) -> None:
    """Fetch, then write. The commit first hands back the connection `_run` opened."""
    await db.commit()
    previews = await asyncio.gather(*(fetch_preview(url) for _, url in links))
    for (link_id, _), preview in zip(links, previews):
        found = preview or {}
        await db.execute(
            text(
                "update public.message_links set title = :title, description = :description,"
                " image_url = :image_url, fetched_at = now() where id = :id"
            ),
            {
                "id": link_id,
                "title": found.get("title"),
                "description": found.get("description"),
                "image_url": found.get("image_url"),
            },
        )
    # So an open thread's next poll picks the previews up through `changed`.
    await db.execute(
        text(
            "update public.messages m set updated_at = now()"
            " from public.message_links l"
            " where l.id = any(cast(:ids as uuid[])) and m.id = l.message_id and m.deleted_at is null"
        ),
        {"ids": [link_id for link_id, _ in links]},
    )


def link_out(r: Any) -> dict[str, Any]:
    return {
        "id": str(r["id"]),
        "url": r["url"],
        "domain": r["domain"],
        "title": r["title"],
        "description": r["description"],
        "image_url": r["image_url"],
    }
