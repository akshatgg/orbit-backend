"""Platform-specific handling for Luma, Meetup, Eventbrite and Fatsoma sources: feed URLs,
feed clean-up, page links (port of `src/lib/sources/platforms.ts`)."""

import re
from dataclasses import dataclass
from typing import Any

from . import js, urls
from .fetch import check_feed_url
from .text import LIMITS, clamp, or_null

DEFAULT_ATTRIBUTION = {
    "luma": "From Luma",
    "luma-discover": "From Luma",
    "meetup": "From Meetup",
    "meetup-search": "From Meetup",
    "eventbrite": "From Eventbrite",
    "fatsoma": "From Fatsoma",
}

LUMA_HOSTS = {"lu.ma", "www.lu.ma", "luma.com", "www.luma.com", "api.lu.ma", "api.luma.com"}
LUMA_FEED_HOSTS = {"api.lu.ma", "api.luma.com"}
LUMA_PAGE_HOSTS = {"lu.ma", "www.lu.ma", "luma.com", "www.luma.com"}

MEETUP_RESERVED = {
    "find", "topics", "cities", "events", "login", "register", "apps", "about", "help", "pro", "lp", "blog",
    "home", "messages", "account", "notifications", "create", "start", "members", "groups",
}
_MEETUP_LOCALE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
_HAS_SCHEME = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.I | re.A)


@dataclass
class FeedUrlResult:
    ok: bool
    url: str | None = None
    error: str | None = None


def parse_loose(value: str) -> urls.URL | None:
    """Parses user input that may lack a scheme ("meetup.com/x")."""
    raw = js.trim(value or "")
    if not raw:
        return None
    return urls.parse(raw if _HAS_SCHEME.match(raw) else f"https://{raw}")


def _segments(url: urls.URL) -> list[str]:
    return [segment for segment in url.pathname.split("/") if segment]


def _final_check(url: str) -> FeedUrlResult:
    check = check_feed_url(url)
    return FeedUrlResult(True, url=check.url.href) if check.ok else FeedUrlResult(False, error=check.error)


def _resolve_meetup(value: str) -> FeedUrlResult:
    url = parse_loose(value)
    host = re.sub(r"^www\.", "", url.hostname.lower()) if url else None
    if not url or host != "meetup.com":
        return FeedUrlResult(False, error="Paste a Meetup group link, e.g. https://www.meetup.com/your-group/")
    segments = _segments(url)
    first = segments[0] if segments else ""
    urlname = (segments[1] if len(segments) > 1 else None) if _MEETUP_LOCALE.match(first) else (segments[0] if segments else None)
    if not urlname or urlname.lower() in MEETUP_RESERVED or not re.match(r"^[A-Za-z0-9-]{2,80}$", urlname):
        return FeedUrlResult(
            False,
            error="That isn't a Meetup group link. Open the group and copy its address, e.g. https://www.meetup.com/your-group/",
        )
    return _final_check(f"https://www.meetup.com/{urlname}/events/ical/")


def _resolve_luma(value: str) -> FeedUrlResult:
    url = parse_loose(re.sub(r"^webcal://", "https://", js.trim(value), flags=re.I))
    host = url.hostname.lower() if url else None
    if not url or not host or host not in LUMA_HOSTS:
        return FeedUrlResult(
            False, error="Paste the calendar's Subscribe → iCal link from Luma (it starts with webcal://api.lu.ma/ics/…)."
        )
    if host not in LUMA_FEED_HOSTS or not url.pathname.startswith("/ics/"):
        return FeedUrlResult(
            False, error="That's a Luma page, not its feed. Paste the calendar's Subscribe → iCal link instead."
        )
    return _final_check(url.href)


LUMA_RESERVED = {
    "event", "events", "discover", "home", "signin", "signup", "create", "manage", "ics", "api",
    "about", "help", "pricing", "blog", "careers", "terms", "privacy", "download", "explore",
    "user", "calendar", "cal",
}
_SLUG = re.compile(r"^[a-z0-9-]{2,80}$")


def luma_discover_slug(value: str) -> str | None:
    """The city slug of a Luma city page, or None."""
    url = parse_loose(js.trim(value or ""))
    if not url or url.hostname.lower() not in LUMA_PAGE_HOSTS:
        return None
    segments = _segments(url)
    if len(segments) != 1:
        return None
    slug = segments[0].lower()
    return slug if _SLUG.match(slug) and slug not in LUMA_RESERVED else None


def _resolve_luma_discover(value: str) -> FeedUrlResult:
    raw = parse_loose(js.trim(value))
    host = raw.hostname.lower() if raw else None
    segments = _segments(raw) if raw else []
    if raw and host and host in LUMA_PAGE_HOSTS and segments and segments[0] == "event" and len(segments) > 1:
        return _final_check(raw.href)
    slug = luma_discover_slug(value)
    if not slug:
        return FeedUrlResult(False, error="Paste a Luma city, category or event link, e.g. https://lu.ma/london.")
    near = raw.search_params.get("near") if raw else None
    if near is not None and not luma_near_slug(value):
        return FeedUrlResult(
            False, error="After near=, put a Luma city page name, e.g. https://luma.com/tech?near=bengaluru."
        )
    suffix = f"?near={js.trim(near).lower()}" if near else ""
    return _final_check(f"https://luma.com/{slug}{suffix}")


def luma_near_slug(value: str) -> str | None:
    """The city for a Luma category link (luma.com/tech?near=bengaluru), or None."""
    url = parse_loose(js.trim(value or ""))
    near = url.search_params.get("near") if url else None
    near = js.trim(near).lower() if near is not None else None
    return near if near and _SLUG.match(near) and near not in LUMA_RESERVED else None


MEETUP_LOCATION_ERROR = (
    "Add a city to this Meetup search link: pick a location on Meetup, then copy the link again. It should "
    "contain &location=…, e.g. https://www.meetup.com/find/?categoryId=546&source=EVENTS&location=gb--London"
)
_FIND = re.compile(r"^/find/?$")


def meetup_find_missing_location(value: str) -> bool:
    """A Meetup find link without `location` (Meetup then centres on wherever the request comes from)."""
    url = parse_loose(value)
    if not url or not _FIND.match(url.pathname):
        return False
    location = url.search_params.get("location")
    return not (location is not None and js.trim(location))


def _resolve_meetup_search(value: str) -> FeedUrlResult:
    url = parse_loose(value)
    host = re.sub(r"^www\.", "", url.hostname.lower()) if url else None
    if not url or host != "meetup.com":
        return FeedUrlResult(False, error="Paste a Meetup event link or search link.")
    is_find = bool(_FIND.match(url.pathname))
    is_event = not url.pathname.startswith("/find/") and bool(re.match(r"^/[^/]+/events/[0-9]+/?$", url.pathname))
    if not is_find and not is_event:
        return FeedUrlResult(
            False, error="Paste a Meetup event link (…/events/123/) or a search link (…/find/?location=…)."
        )
    if is_find and meetup_find_missing_location(url.href):
        return FeedUrlResult(False, error=MEETUP_LOCATION_ERROR)
    return _final_check(url.href)


# ─── Eventbrite ───────────────────────────────────────────────────────────

_EVENTBRITE_HOST = re.compile(r"^(?:www\.)?eventbrite\.[a-z]{2,3}(?:\.[a-z]{2})?$")
EVENTBRITE_ORIGIN = "https://www.eventbrite.co.uk"
_EVENTBRITE_SLUG = re.compile(r"^[a-z0-9-]+$", re.I | re.A)
_TRAILING_ID = re.compile(r"(?:^|-)([0-9]{5,20})$")


@dataclass
class EventbriteLink:
    type: str  # organizer | event | search
    url: str
    id: str | None = None


def eventbrite_link(value: str) -> EventbriteLink | None:
    """What an Eventbrite link points at: an organiser (/o/…), one event (/e/…) or a search (/d/…)."""
    url = parse_loose(value)
    if not url or not _EVENTBRITE_HOST.match(url.hostname.lower()):
        return None
    segments = _segments(url)
    section = segments[0] if segments else None
    slug = segments[1] if len(segments) > 1 else None
    rest = segments[2:]
    if section == "o" and slug and not rest:
        match = _TRAILING_ID.search(slug)
        return EventbriteLink("organizer", f"{EVENTBRITE_ORIGIN}/o/{slug}", match.group(1)) if match else None
    if section == "e" and slug and not rest:
        match = _TRAILING_ID.search(slug)
        return EventbriteLink("event", f"{EVENTBRITE_ORIGIN}/e/{slug}", match.group(1)) if match else None
    if section == "d" and slug and _EVENTBRITE_SLUG.match(slug) and len(rest) <= 1:
        path = url.pathname if url.pathname.endswith("/") else f"{url.pathname}/"
        return EventbriteLink("search", f"{EVENTBRITE_ORIGIN}{path}{url.search}")
    if section == "b" and slug and _EVENTBRITE_SLUG.match(slug) and len(rest) == 1 and _EVENTBRITE_SLUG.match(rest[0]):
        return EventbriteLink("search", f"{EVENTBRITE_ORIGIN}/d/{slug}/{rest[0].lower()}--events/")
    return None


def _resolve_eventbrite(value: str) -> FeedUrlResult:
    link = eventbrite_link(value)
    if not link:
        return FeedUrlResult(
            False,
            error="Paste an Eventbrite organiser page (…/o/name-123), event page (…/e/name-tickets-123), category "
            "page (…/b/united-kingdom--london/music/) or search page (…/d/united-kingdom--london/events/).",
        )
    return _final_check(link.url)


# ─── Fatsoma ──────────────────────────────────────────────────────────────

FATSOMA_HOSTS = {"fatsoma.com", "www.fatsoma.com"}
_FATSOMA_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,99}$", re.I | re.A)
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I | re.A)
_LAT_LON = re.compile(r"^(-?[0-9]{1,3}(?:\.[0-9]+)?)," + js.S + r"*(-?[0-9]{1,3}(?:\.[0-9]+)?)$")


@dataclass
class FatsomaLink:
    type: str  # page | event | city | discover
    url: str
    vanity: str | None = None
    category_id: str | None = None
    lat_lon: tuple[float, float] | None = None


def _lat_lon_param(value: str | None) -> tuple[float, float] | None:
    """ "51.5072178,-0.1275862" → (lat, lon), or None when it isn't a point on the map."""
    match = _LAT_LON.match(js.trim(value)) if value is not None else None
    if not match:
        return None
    lat, lon = float(match.group(1)), float(match.group(2))
    return (lat, lon) if abs(lat) <= 90 and abs(lon) <= 180 else None


def _fatsoma_discover(url: urls.URL) -> FatsomaLink | None:
    params = url.search_params
    raw_category = params.get("category-id")
    raw_lat_lon = params.get("lat-lng")
    category_id = raw_category.lower() if raw_category and UUID_RE.match(raw_category) else None
    lat_lon = _lat_lon_param(raw_lat_lon)
    if (raw_category and not category_id) or (raw_lat_lon and not lat_lon) or (not category_id and not lat_lon):
        return None
    clean = urls.parse("https://www.fatsoma.com/discover")
    assert clean is not None
    out = clean.search_params
    if category_id:
        out.set("category-id", category_id)
    if lat_lon:
        out.set("lat-lng", ",".join(js.num_str(n) for n in lat_lon))
        place = params.get("location")
        place = js.u16slice(js.trim(place), 100) if place is not None else None
        if place:
            out.set("location", place)
    return FatsomaLink("discover", clean.href, category_id=category_id, lat_lon=lat_lon)


def fatsoma_link(value: str) -> FatsomaLink | None:
    """What a Fatsoma link points at: an organiser, one event, a city or a Discover search."""
    url = parse_loose(value)
    if not url or url.hostname.lower() not in FATSOMA_HOSTS:
        return None
    segments = _segments(url)
    if len(segments) == 1 and segments[0] == "discover":
        return _fatsoma_discover(url)
    section = segments[0] if segments else None
    first = segments[1] if len(segments) > 1 else None
    second = segments[2] if len(segments) > 2 else None
    if not first or not _FATSOMA_SLUG.match(first):
        return None
    if section == "p" and len(segments) <= 3:
        return FatsomaLink("page", f"https://www.fatsoma.com/p/{first}", vanity=first)
    if section == "e" and len(segments) <= 3 and (not second or _FATSOMA_SLUG.match(second)):
        tail = f"/{second}" if second else ""
        return FatsomaLink("event", f"https://www.fatsoma.com/e/{first}{tail}", vanity=first)
    if section == "l" and second and _FATSOMA_SLUG.match(second) and len(segments) == 3 and first != "locations":
        return FatsomaLink("city", f"https://www.fatsoma.com/l/{first.lower()}/{second.lower()}")
    return None


def _resolve_fatsoma(value: str) -> FeedUrlResult:
    link = fatsoma_link(value)
    if not link:
        return FeedUrlResult(
            False,
            error="Paste a Fatsoma organiser page (…/p/name), event page (…/e/abc123/name), city page (…/l/gb/london) "
            "or Discover page with a category or location picked (…/discover?category-id=…).",
        )
    return _final_check(link.url)


def resolve_feed_url(kind: str, value: str) -> FeedUrlResult:
    """Turns what an admin pasted into the feed URL Orbit fetches."""
    resolvers = {
        "meetup": _resolve_meetup,
        "meetup-search": _resolve_meetup_search,
        "luma": _resolve_luma,
        "luma-discover": _resolve_luma_discover,
        "eventbrite": _resolve_eventbrite,
        "fatsoma": _resolve_fatsoma,
    }
    resolver = resolvers.get(kind)
    return resolver(value) if resolver else _final_check(value)


# Luma's feed DESCRIPTION is only a stub:
#   "Get up-to-date information at: https://luma.com/<slug>\n\nAddress:\n…\n\nHosted by <host>"
_LUMA_LINK = re.compile(r"https://(?:www\.)?(?:luma\.com|lu\.ma)/[A-Za-z0-9_-]+")
_HOSTED_BY = re.compile(r"(?:^|(?<=[\n\r  ]))Hosted by ([^\n\r  ]+)(?=[\n\r  ]|\Z)")


def clean_event(kind: str, event: dict[str, Any]) -> dict[str, Any]:
    """Platform tidy-up after parsing a feed. Other kinds pass through unchanged."""
    if kind not in ("luma", "luma-discover"):
        return event
    stub = event.get("description") or ""
    hosted = _HOSTED_BY.search(stub)
    host = js.trim(hosted.group(1)) if hosted else None
    link = _LUMA_LINK.search(stub)
    return {
        **event,
        "url": event.get("url") or (link.group(0) if link else None),
        "description": None,
        "summary": or_null(clamp(f"Hosted by {host}", LIMITS["summary"])) if host else None,
    }


MEETUP_PAGE_HOSTS = {"meetup.com", "www.meetup.com"}
_MEETUP_EVENT_PATH = re.compile(r"/events/(?:[0-9]+|[a-z]{10,})/?$")


def meetup_page_url(event: dict[str, Any]) -> str | None:
    """The Meetup event page to read for location details, or None when it isn't one."""
    external = event.get("external_url")
    if not external:
        return None
    url = urls.parse(external)
    if url is None or url.protocol != "https:" or url.hostname.lower() not in MEETUP_PAGE_HOSTS:
        return None
    return url.href if _MEETUP_EVENT_PATH.search(url.pathname) else None


def luma_page_url(event: dict[str, Any]) -> str | None:
    """The Luma page to read for an imported event, or None when it isn't a Luma event page."""
    source_event_id = event.get("source_event_id")
    external = event.get("external_url")
    if not (isinstance(source_event_id, str) and source_event_id.startswith("evt-")) or not external:
        return None
    url = urls.parse(external)
    if url is None:
        return None
    return url.href if url.protocol == "https:" and url.hostname.lower() in LUMA_PAGE_HOSTS else None


PLATFORM_IMAGE_HOSTS = {
    "eventbrite": {"img.evbuc.com", "cdn.evbuc.com"},
    "fatsoma": {"cdn2.fatsoma.com", "fatsoma.imgix.net"},
}


def platform_image(kind: str, value: Any) -> str | None:
    """An https image on the platform's own hosts (accepted on import), or None."""
    if not isinstance(value, str):
        return None
    url = urls.parse(value)
    if url is None:
        return None
    hosts = PLATFORM_IMAGE_HOSTS.get(kind)
    return url.href if url.protocol == "https:" and hosts and url.hostname in hosts else None


def is_platform_hosted_image(kind: str, value: str | None) -> bool:
    return bool(value) and platform_image(kind, value) is not None
