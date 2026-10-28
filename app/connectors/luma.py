"""Luma: discover pages (city feeds, category listings), single event pages and the page reader
used by enrichment (port of `luma-discover.ts` and `luma-page.ts`)."""

import json
import re
from datetime import datetime
from typing import Any

from . import fetch as http
from . import ical, js, urls
from .base import HINT_WEIGHT, ConnectorError, FetchedFeed, covered_until
from .jsonld import address_country, first_json_ld_event, parse_json
from .platforms import clean_event, luma_discover_slug, luma_near_slug
from .polite import PoliteClient, fetch_html
from .text import LIMITS, clamp, clean_prose, decode_entities, one_line, or_null, safe_http_url, strip_html, summarise
from .timezone import is_valid_timezone, valid_or_london

# ─── Discover pages ───────────────────────────────────────────────────────────

_DISCOVER_NEXT_DATA = re.compile(r"<script id=\"__NEXT_DATA__\"[^>]*>([\s\S]*?)</script>")
_PAGE_NEXT_DATA = re.compile(r"<script[^>]*id=[\"']__NEXT_DATA__[\"'][^>]*>([\s\S]*?)</script>", re.IGNORECASE | re.ASCII)


def _obj(value: Any) -> Any:
    """`value && typeof value === "object" ? value : null` (arrays count, as in JS)."""
    return value if js.is_object(value) else None


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _iterate(value: Any) -> list[Any]:
    """`for (const x of value ?? [])`: arrays iterate, strings by character, objects throw."""
    if value is None or value is js.MISSING:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return list(value)
    raise TypeError("value is not iterable")


def _initial_data(html: str) -> Any:
    """The page's `initialData` from __NEXT_DATA__, or None."""
    match = _DISCOVER_NEXT_DATA.search(html)
    if not match or not match.group(1):
        return None
    data = parse_json(match.group(1))
    if data is js.MISSING:
        return None
    value = js.get(js.get(js.get(data, "props"), "pageProps"), "initialData")
    return None if value is None or value is js.MISSING else value


def parse_luma_discover_page(html: str, slug: str | None) -> dict[str, Any] | None:
    """The `discplace-…` feed for a city page (current and older page shapes)."""
    initial = _initial_data(html)
    if not js.truthy(initial):
        return None
    candidates: list[Any] = []

    def add(value: Any) -> None:
        item = _obj(value)
        if item is not None:
            candidates.append(item)

    add(js.get(js.get(initial, "data"), "place"))
    add(js.get(js.get(initial, "featured_place"), "place"))
    for item in _iterate(js.get(initial, "places")):
        add(js.get(item, "place"))

    if slug:
        chosen = next((item for item in candidates if js.get(item, "slug") == slug), None)
    else:
        chosen = candidates[0] if candidates else None
    if chosen is None:
        return None
    api_id = js.get(chosen, "api_id")
    if not isinstance(api_id, str) or not api_id.startswith("discplace-"):
        return None
    chosen_slug = js.get(chosen, "slug")
    name = js.get(chosen, "name")
    return {
        "api_id": api_id,
        "slug": chosen_slug if isinstance(chosen_slug, str) else slug,
        "name": name if isinstance(name, str) else None,
        "timezone": valid_or_london(js.get(chosen, "timezone")),
        "feed_url": f"https://api.lu.ma/ics/get?entity=discover&id={api_id}",
    }


# ─── Category pages ───────────────────────────────────────────────────────────

LUMA_CATEGORY_PAGE_SIZE = 50
LUMA_CATEGORY_MAX_PAGES = 5


def parse_luma_category_page(html: str) -> dict[str, Any] | None:
    """The category on a category page (lu.ma/tech → `cat-tech`, "Tech"), or None."""
    initial = _initial_data(html)
    category = _obj(js.get(js.get(initial, "data"), "category")) if js.get(initial, "kind") == "category" else None
    api_id = js.get(category, "api_id")
    if not isinstance(api_id, str) or not api_id.startswith("cat-"):
        return None
    slug = js.get(category, "slug")
    name = js.get(category, "name")
    return {
        "api_id": api_id,
        "slug": slug if isinstance(slug, str) else None,
        "name": js.trim(name) if isinstance(name, str) and js.trim(name) else api_id[4:],
    }


def luma_category_events_url(category_id: str, place_id: str, cursor: str | None) -> str:
    """One page of a category's upcoming events in one city, soonest first."""
    params = urls.SearchParams(pairs=[
        ("discover_category_api_id", category_id),
        ("discover_place_api_id", place_id),
        ("pagination_limit", str(LUMA_CATEGORY_PAGE_SIZE)),
    ])
    if cursor:
        params.set("pagination_cursor", cursor)
    return f"https://api.lu.ma/discover/get-paginated-events?{params}"


_LUMA_IMAGE_HOSTS = {"cdn.lu.ma", "images.lumacdn.com"}


def _luma_image(value: Any) -> str | None:
    url = urls.parse(_text(value))
    if url is None:
        return None
    return url.href if url.protocol == "https:" and url.hostname in _LUMA_IMAGE_HOSTS else None


def _cents(value: Any) -> float | None:
    n = js.number(value)
    return n / 100 if value is not None and value is not js.MISSING and js.finite(n) and n >= 0 else None


_CURRENCY = re.compile(r"^[A-Z]{3}\Z")


def _ticket_price(info: Any) -> dict[str, Any] | None:
    """ "Free", a fixed price, or a pay-what-you-want range; None when Luma doesn't say."""
    if js.get(info, "is_free") is True:
        return {"min": 0, "max": 0, "currency": "GBP"}  # GBP even outside the UK, as the TS engine stored it
    price = _obj(js.get(info, "price"))
    amount = _cents(js.get(price, "cents"))
    currency = _text(js.get(price, "currency")).upper()
    if amount is None or not _CURRENCY.match(currency):
        return None
    if js.get(price, "is_flexible") is True:
        flexible = _cents(js.get(price, "min_cents"))
        low = flexible if flexible is not None else amount
    else:
        low = amount
    top = _cents(js.get(_obj(js.get(info, "max_price")), "cents"))
    high = top if top is not None else amount
    return {"min": min(low, high), "max": max(low, high), "currency": currency}


def _host_line(hosts: list[str]) -> str:
    """ "A", "A & B", or "A, B and 3 others"."""
    if len(hosts) <= 2:
        return " & ".join(hosts)
    others = len(hosts) - 2
    return f"{hosts[0]}, {hosts[1]} and {others} other{'' if others == 1 else 's'}"


_SLUG = re.compile(r"^[A-Za-z0-9_-]+\Z")


def _category_event(entry: Any, category: dict[str, Any]) -> dict[str, Any]:
    """One entry of Luma's paginated events list as an Orbit event, or a skip reason."""
    event = _obj(js.get(entry, "event"))
    api_id = _text(js.get(event, "api_id"))
    if event is None or not api_id.startswith("evt-"):
        return {"message": "Skipped: Luma entry without an event id"}

    title = clamp(one_line(_text(js.get(event, "name"))), LIMITS["title"])
    if js.u16len(title) < 3:
        return {"event": api_id, "message": "Skipped: title is missing or too short"}
    starts = js.parse_date(_text(js.get(event, "start_at")))
    if starts is None:
        return {"event": api_id, "message": "Skipped: invalid start date"}
    ends = js.parse_date(_text(js.get(event, "end_at"))) if js.truthy(js.get(event, "end_at")) else None
    slug = _text(js.get(event, "url"))
    if not _SLUG.match(slug):
        return {"event": api_id, "message": "Skipped: no Luma page"}

    # Luma hides the exact place of some events until you register; only the city is public then.
    geo = _obj(js.get(event, "geo_address_info"))
    shown = js.get(geo, "mode") == "shown"
    point = _obj(js.get(event, "coordinate"))
    latitude = js.number(js.get(point, "latitude"))
    longitude = js.number(js.get(point, "longitude"))
    has_point = (
        shown and js.finite(latitude) and js.finite(longitude) and abs(latitude) <= 90 and abs(longitude) <= 180
    )
    hosts_raw = js.get(entry, "hosts")
    hosts = [one_line(_text(js.get(_obj(h), "name"))) for h in (hosts_raw if isinstance(hosts_raw, list) else [])]
    hosts = [h for h in hosts if h]

    return {
        "source_event_id": f"{api_id}@events.lu.ma",
        "title": title,
        "summary": or_null(clamp(f"Hosted by {_host_line(hosts)}", LIMITS["summary"])) if hosts else None,
        "description": None,
        "starts_at": js.iso(starts),
        "ends_at": js.iso(ends) if ends is not None else None,
        "all_day": False,
        "timezone": valid_or_london(_text(js.get(event, "timezone"))),
        "format": "in_person" if js.get(event, "location_type") == "offline" else "online",
        "venue_name": or_null(clamp(one_line(_text(js.get(geo, "address"))), LIMITS["venue"])) if shown else None,
        "address": or_null(clamp(one_line(_text(js.get(geo, "short_address"))), LIMITS["address"])) if shown else None,
        "city": or_null(clamp(one_line(_text(js.get(geo, "city"))), LIMITS["venue"])),
        "country": or_null(_text(js.get(geo, "country_code"))),
        "latitude": latitude if has_point else None,
        "longitude": longitude if has_point else None,
        "url": f"https://luma.com/{slug}",
        "cancelled": False,
        "interest_hints": [{"label": category["name"], "weight": HINT_WEIGHT["eventCategory"]}],
        "image_url": _luma_image(js.get(event, "cover_url")),
        "price": _ticket_price(_obj(js.get(entry, "ticket_info"))),
    }


def parse_luma_category_events(body: str, category: dict[str, Any]) -> dict[str, Any]:
    """One page of Luma's paginated events response. Raises when it isn't that shape."""
    data = _obj(json.loads(body))  # a JSON syntax error raises, as JSON.parse does
    entries = js.get(data, "entries")
    if data is None or not isinstance(entries, list):
        raise ConnectorError("Luma's event list came back in an unexpected shape.")
    events: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for entry in entries:
        result = _category_event(entry if js.is_object(entry) else {}, category)
        if "source_event_id" in result:
            events.append(result)
        else:
            errors.append(result)
    next_cursor = _text(js.get(data, "next_cursor")) or None
    return {
        "events": events,
        "errors": errors,
        "has_more": js.get(data, "has_more") is True and next_cursor is not None,
        "next_cursor": next_cursor,
    }


# ─── Event pages ──────────────────────────────────────────────────────────────

_PAGE_IMAGE_HOSTS = {"cdn.lu.ma", "images.lumacdn.com"}


def _safe_image(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    url = urls.parse(value)
    if url is None:
        return None
    return url.href if url.protocol == "https:" and url.hostname in _PAGE_IMAGE_HOSTS else None


def _page_data(html: str) -> Any:
    """The record in the page's __NEXT_DATA__ that holds `description_mirror`."""
    match = _PAGE_NEXT_DATA.search(html)
    if not match or not match.group(1):
        return None
    root = parse_json(match.group(1))
    if root is js.MISSING:
        return None

    def find(value: Any, depth: int) -> Any:
        if not js.is_object(value) or depth > 12:
            return None
        if isinstance(value, dict) and "description_mirror" in value:
            return value
        for child in value.values() if isinstance(value, dict) else value:
            found = find(child, depth + 1)
            if found is not None:
                return found
        return None

    return find(root, 0)


_ZERO_WIDTH = re.compile("[\u200b-\u200d\ufeff]")


def _tidy(value: str) -> str:
    return _ZERO_WIDTH.sub("", value).replace("\u00a0", " ")


_SURROUND = re.compile(f"^({js.S}*)([\\s\\S]*?)({js.S}*)\\Z")


def _surround(value: str) -> tuple[str, str, str]:
    match = _SURROUND.match(value)
    assert match is not None
    return match.group(1), match.group(2), match.group(3)


def _mirror_inline(nodes: Any = js.MISSING, plain: bool = False) -> str:
    """Inline content in the stored description format: `**bold**`, `*italic*`, "label (url)"."""
    runs: list[dict[str, Any]] = []
    for node in _iterate(nodes):
        if js.get(node, "type") == "hard_break":
            runs.append({"text": "\n", "bold": False, "italic": False})
            continue
        node_text = js.get(node, "text")
        if not isinstance(node_text, str):
            content = js.get(node, "content")
            if js.truthy(content):
                runs.append({"text": _mirror_inline(content, plain), "bold": False, "italic": False})
            continue
        marks = js.get(node, "marks")
        marks = [] if marks is None or marks is js.MISSING else marks
        link_mark = next((m for m in marks if js.get(m, "type") == "link"), None)
        href = js.get(js.get(link_mark, "attrs"), "href")
        link = safe_http_url(href) if isinstance(href, str) else None
        value = _tidy(node_text)
        lead, core, trail = _surround(value)
        if link and core and core != link and core != href:
            value = f"{lead}{core} ({link}){trail}"
        bold = not plain and any(js.get(m, "type") in ("bold", "strong") for m in marks)
        italic = not plain and any(js.get(m, "type") in ("italic", "em") for m in marks)
        last = runs[-1] if runs else None
        if last and last["bold"] == bold and last["italic"] == italic and last["text"] != "\n":
            last["text"] += value
        else:
            runs.append({"text": value, "bold": bold, "italic": italic})
    out = []
    for run in runs:
        marker = "**" if run["bold"] else "*" if run["italic"] else ""
        if not marker or not js.trim(run["text"]):
            out.append(run["text"])
            continue
        lead, core, trail = _surround(run["text"])
        out.append(f"{lead}{marker}{core}{marker}{trail}")
    return "".join(out)


_SPACES = re.compile(f"{js.S}+")
_SPACE_BEFORE_NEWLINE = re.compile(r"[ \t]+\n")
_NEWLINE_RUN = re.compile(f"{js.S}*\\n{js.S}*")
_BLANKS = re.compile(r"\n{3,}")


def _mirror_blocks(nodes: Any = js.MISSING) -> list[str]:
    """Block content as stored-description text: blank lines between blocks, `•` / `1.` lists."""
    blocks: list[str] = []
    for node in _iterate(nodes):
        kind = js.get(node, "type")
        if kind == "heading":
            text = js.trim(_SPACES.sub(" ", _mirror_inline(js.get(node, "content"), plain=True)))
            if text:
                blocks.append(f"**{text}**")
        elif kind == "paragraph":
            text = js.trim(_SPACE_BEFORE_NEWLINE.sub("\n", _mirror_inline(js.get(node, "content"))))
            if text:
                blocks.append(text)
        elif kind in ("bullet_list", "ordered_list"):
            attrs = js.get(node, "attrs")
            order = js.get(attrs, "order")
            if order is None or order is js.MISSING:
                order = js.get(attrs, "start")
            if order is None or order is js.MISSING:
                order = 1
            start = js.number(order)
            lines = []
            content = js.get(node, "content")
            for i, item in enumerate(_iterate(None if content is js.MISSING else content)):
                text = js.trim(_NEWLINE_RUN.sub(" ", " ".join(_mirror_blocks(js.get(item, "content")))))
                bullet = f"{js.num_str((start if js.finite(start) else 1) + i)}." if kind == "ordered_list" else "•"
                lines.append(f"{bullet} {text}" if text else "")
            listing = "\n".join(line for line in lines if line)
            if listing:
                blocks.append(listing)
        elif kind == "horizontal_rule":
            continue
        else:
            # blockquote, list_item and anything new: keep its text.
            blocks.extend(_mirror_blocks(js.get(node, "content")))
    return blocks


def luma_mirror_description(html: str) -> str | None:
    """The host's formatted description from Luma's page data, or None."""
    mirror = js.get(_page_data(html), "description_mirror")
    if not js.is_object(mirror) or js.get(mirror, "type") != "doc":
        return None
    joined = js.trim(_BLANKS.sub("\n\n", "\n\n".join(_mirror_blocks(js.get(mirror, "content")))))
    return or_null(clamp(joined, LIMITS["description"]))


def _luma_hints(html: str) -> list[dict[str, Any]]:
    """Luma's own categories for the event ("Tech", "AI"…): strong evidence."""
    categories = js.get(_page_data(html), "categories")
    labels = [js.get(c, "name") for c in (categories if isinstance(categories, list) else [])]
    return [{"label": label, "weight": HINT_WEIGHT["eventCategory"]} for label in labels if isinstance(label, str) and label]


def _json_ld_description(event: Any) -> str | None:
    """The JSON-LD description, cleaned the way every Luma import used to store it."""
    description = js.get(event, "description")
    raw = description if isinstance(description, str) else ""
    return or_null(clamp(clean_prose(strip_html(raw), keep_emphasis=True), LIMITS["description"]))


def _page_description(html: str, event: Any) -> str | None:
    mirror = luma_mirror_description(html)
    return mirror if mirror is not None else _json_ld_description(event)


_OG_TAG = re.compile(r"<meta[^>]+property=[\"']og:image[\"'][^>]*>", re.IGNORECASE | re.ASCII)
_CONTENT = re.compile(r"content=[\"']([^\"']+)[\"']", re.IGNORECASE | re.ASCII)


def _og_image(html: str) -> str | None:
    tag = _OG_TAG.search(html)
    content = _CONTENT.search(tag.group(0)) if tag else None
    return _safe_image(decode_entities(content.group(1))) if content else None


def _prices(offers: Any) -> dict[str, Any] | None:
    """A price range only when every offer has a number and they share one currency."""
    items = offers if isinstance(offers, list) else [offers] if js.truthy(offers) else []
    if not items:
        return None
    values: list[float] = []
    currency: str | None = None
    for offer in items:
        raw = js.get(offer, "price")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            price = float(raw)
        elif isinstance(raw, str) and js.trim(raw) != "":
            price = js.number(raw)
        else:
            price = float("nan")
        if not js.finite(price) or price < 0:
            return None
        code_raw = js.get(offer, "priceCurrency")
        code = js.trim(code_raw).upper() if isinstance(code_raw, str) else ""
        if not code or (currency and code != currency):
            return None
        currency = code
        values.append(price)
    return {"price_min": min(values), "price_max": max(values), "currency": currency}


_TIMEZONE = re.compile(r"\"timezone\":\"([^\"]+)\"")
_API_ID = re.compile(r"\"api_id\":\"(evt-[^\"]+)\"")


def _location_parts(event: Any) -> tuple[Any, Any, dict[str, Any] | None]:
    location = js.get(event, "location")
    location = location if js.truthy(location) and js.is_object(location) else None
    address = js.get(location, "address")
    address = address if js.truthy(address) and js.is_object(address) else None
    geo_raw = js.get(location, "geo")
    geo = geo_raw if js.truthy(geo_raw) and js.is_object(geo_raw) else location
    latitude = js.number(js.get(geo, "latitude"))
    longitude = js.number(js.get(geo, "longitude"))
    point = (
        {"latitude": latitude, "longitude": longitude}
        if js.finite(latitude) and js.finite(longitude) and abs(latitude) <= 90 and abs(longitude) <= 180
        else None
    )
    return location, address, point


def _first_image(event: Any) -> str | None:
    image = js.get(event, "image")
    for value in image if isinstance(image, list) else [image]:
        found = _safe_image(value)
        if found is not None:
            return found
    return None


def parse_luma_linked_event(html: str, page_url: str) -> dict[str, Any] | None:
    """One Luma event page as an Orbit event (an admin pasted a single event link)."""
    event = first_json_ld_event(html)
    if event is None:
        return None
    name = js.get(event, "name")
    title = clamp(one_line(name if isinstance(name, str) else ""), LIMITS["title"])
    start_date = js.get(event, "startDate")
    starts = js.parse_date(start_date) if isinstance(start_date, str) else None
    end_date = js.get(event, "endDate")
    ends = js.parse_date(end_date) if isinstance(end_date, str) and end_date else None
    event_url = js.get(event, "url")
    url = safe_http_url(event_url if isinstance(event_url, str) else page_url) or safe_http_url(page_url)
    if js.u16len(title) < 3 or starts is None or not url:
        return None

    location, address, point = _location_parts(event)
    mode = js.get(event, "eventAttendanceMode")
    mode = mode if isinstance(mode, str) else ""
    status = js.get(event, "eventStatus")
    status = status if isinstance(status, str) else ""
    description = _page_description(html, event)
    tz_match = _TIMEZONE.search(html)
    timezone = tz_match.group(1) if tz_match else None
    parsed = urls.parse(url)
    segments = [s for s in parsed.pathname.split("/") if s] if parsed else []
    slug = segments[-1] if segments else None
    api_match = _API_ID.search(html)

    def field(source: Any, key: str, limit: int) -> str | None:
        value = js.get(source, key)
        return or_null(clamp(one_line(value if isinstance(value, str) else ""), limit))

    image = _first_image(event)
    return {
        "source_event_id": api_match.group(1) if api_match else (f"link-{slug}" if slug else "link-event"),
        "title": title,
        "summary": summarise(description),
        "description": description,
        "starts_at": js.iso(starts),
        "ends_at": js.iso(ends) if ends is not None else None,
        "all_day": False,
        "timezone": timezone if timezone and is_valid_timezone(timezone) else "Europe/London",
        "format": "online" if "Online" in mode else "hybrid" if "Mixed" in mode else "in_person",
        "venue_name": field(location, "name", LIMITS["venue"]),
        "address": field(address, "streetAddress", LIMITS["address"]),
        "city": field(address, "addressLocality", LIMITS["venue"]),
        "country": address_country(address),
        "latitude": point["latitude"] if point else None,
        "longitude": point["longitude"] if point else None,
        "url": url,
        "cancelled": "Cancelled" in status,
        "interest_hints": _luma_hints(html),
        "image_url": image if image is not None else _og_image(html),
    }


def parse_luma_event_page(html: str) -> dict[str, Any] | None:
    """Cover, full description and price from a Luma event page, or None when it has none."""
    event = first_json_ld_event(html)
    cover_url = _first_image(event) if event is not None else None
    if cover_url is None:
        cover_url = _og_image(html)
    description = _page_description(html, event)
    price = _prices(js.get(event, "offers")) if event is not None else None
    location, address, point = _location_parts(event)

    def field(source: Any, key: str, limit: int) -> str | None:
        return or_null(clamp(_text(js.get(source, key)), limit))

    venue_name = field(location, "name", LIMITS["venue"])
    street = field(address, "streetAddress", LIMITS["address"])
    city = field(address, "addressLocality", LIMITS["venue"])
    hints = _luma_hints(html)
    if not cover_url and not description and not price and not venue_name and not street and not point and not hints:
        return None
    return {
        "cover_url": cover_url,
        "description": description,
        "price_min": price["price_min"] if price else None,
        "price_max": price["price_max"] if price else None,
        "currency": price["currency"] if price else None,
        "venue_name": venue_name,
        "address": street,
        "city": city,
        "country": address_country(address),
        "latitude": point["latitude"] if point else None,
        "longitude": point["longitude"] if point else None,
        "flat_description": _json_ld_description(event),
        "interest_hints": hints,
    }


# ─── Connector ────────────────────────────────────────────────────────────────


async def _category_events(category: dict[str, Any], near: str | None) -> FetchedFeed:
    """A category's events near one city (luma.com/tech?near=bengaluru), soonest first, capped."""
    example = f"https://luma.com/{category['slug'] if category['slug'] is not None else 'tech'}?near=bengaluru"
    if not near:
        raise ConnectorError(
            f"Luma's {category['name']} page shows events near whoever opens it. Add the city to the link, e.g. {example}"
        )
    client = PoliteClient()
    place = parse_luma_discover_page(await client.html(f"https://luma.com/{near}"), near)
    if place is None:
        raise ConnectorError(f"Luma has no city page called “{near}”. Use the name from the city's link, e.g. {example}")

    events: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    cursor: str | None = None
    for _page in range(LUMA_CATEGORY_MAX_PAGES):
        body = await client.json(luma_category_events_url(category["api_id"], place["api_id"], cursor))
        result = parse_luma_category_events(body, category)
        events.extend(result["events"])
        errors.extend(result["errors"])
        if not result["has_more"]:
            return FetchedFeed(events=events, errors=errors)
        cursor = result["next_cursor"]
    return FetchedFeed(events=events, errors=errors, coverage=covered_until(events))


class LumaDiscoverConnector:
    """A Luma city page (its public iCal feed), category page (Luma's listing API) or one event."""

    async def fetch(self, source: dict[str, Any], now: datetime) -> FetchedFeed:
        url = source["url"]
        slug = luma_discover_slug(url)
        html = await fetch_html(url)
        place = parse_luma_discover_page(html, slug)
        category = None if place else parse_luma_category_page(html)
        if category:
            return await _category_events(category, luma_near_slug(url))
        if place is None:
            single = parse_luma_linked_event(html, url)
            if single is None:
                raise ConnectorError("Couldn't read that Luma link. Use a city page or an event page.")
            return FetchedFeed(events=[single], errors=[])

        text = await http.fetch_feed_text(place["feed_url"])
        result = ical.parse_ical(text, now=now, default_timezone=place["timezone"])
        return FetchedFeed(
            events=[clean_event("luma-discover", e) for e in result.events],
            errors=result.errors,
            coverage=result.coverage,
        )
