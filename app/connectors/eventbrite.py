"""Eventbrite: the JSON its own website loads (organiser listings, search results, event details).

Port of `src/lib/sources/eventbrite.ts` and the Eventbrite connector in `connectors.ts`.

Terms note: Eventbrite only allows automated access through its official API. The product
owner accepted this exception; keep this source opt-in, capped and easy to disable.
"""

import re
from datetime import datetime
from typing import Any

from . import js, urls
from .base import HINT_WEIGHT, ConnectorError, FetchedFeed, covered_until
from .jsonld import ld_blocks, parse_json
from .platforms import EVENTBRITE_ORIGIN, eventbrite_link, platform_image
from .polite import PoliteClient
from .text import LIMITS, clamp, html_to_prose, one_line, or_null, safe_http_url, summarise
from .timezone import DEFAULT_TIMEZONE, is_valid_timezone, zoned_time_to_utc

EVENTBRITE_BATCH = 50  # ids per details request (the endpoint's page size)
EVENTBRITE_MAX_EVENTS = 250
EVENTBRITE_MAX_PAGES = 5  # organiser listing / search pages

API = f"{EVENTBRITE_ORIGIN}/api/v3"
_ID = re.compile(r"^[0-9]{5,20}$")


def eventbrite_source_id(event_id: str) -> str:
    """`eventbrite-<id>`: the source event id of an imported Eventbrite event."""
    return f"eventbrite-{event_id}"


def eventbrite_id_of(source_event_id: str | None) -> str | None:
    if isinstance(source_event_id, str) and source_event_id.startswith("eventbrite-") and _ID.match(source_event_id[11:]):
        return source_event_id[11:]
    return None


def _url(base: str) -> urls.URL:
    url = urls.parse(base)
    if url is None:
        raise ConnectorError(f"Invalid URL: {base}")
    return url


def organizer_events_url(organizer_id: str, continuation: str | None) -> str:
    """One page of an organiser's live events, soonest first."""
    url = _url(f"{API}/organizers/{organizer_id}/events/")
    params = url.search_params
    params.set("status", "live")
    params.set("order_by", "start_asc")
    if continuation:
        params.set("continuation", continuation)
    return url.href


def event_details_url(ids: list[str]) -> str:
    """Details for up to 50 events, in the shape Eventbrite's own search results use."""
    url = _url(f"{API}/destination/events/")
    params = url.search_params
    params.set("event_ids", ",".join(ids))
    params.set("expand", "image,primary_venue,ticket_availability,primary_organizer")
    params.set("page_size", str(EVENTBRITE_BATCH))
    return url.href


def structured_content_url(event_id: str) -> str:
    """The event's description as its organiser laid it out."""
    return f"{API}/events/{event_id}/structured_content/?purpose=listing"


def _record(value: Any) -> dict | None:
    return value if isinstance(value, dict) else None


def _text(value: Any) -> str | None:
    return js.trim(value) if isinstance(value, str) and js.trim(value) else None


_EVENT_LINK = re.compile(r"/e/(?:[^/?#]*-)?([0-9]{5,20})/?(?:[?#]|\Z)")


def _event_id_from_url(value: Any) -> str | None:
    url = safe_http_url(value if isinstance(value, str) else None)
    match = _EVENT_LINK.search(url) if url else None
    return match.group(1) if match else None


def search_page_url(search_url: str, page: int) -> str:
    """Page `page` of a search (page 1 is asked for too, so a category search isn't redirected)."""
    url = _url(search_url)
    url.search_params.set("page", str(page))
    return url.href


def parse_eventbrite_search_page(html: str) -> list[str]:
    """Event ids from a search page's JSON-LD list."""
    ids: list[str] = []
    for block in ld_blocks(html):
        data = _record(parse_json(block))
        elements = js.get(data, "itemListElement")
        if js.get(data, "@type") != "ItemList" or not isinstance(elements, list):
            continue
        for element in elements:
            event_id = _event_id_from_url(js.get(_record(js.get(_record(element), "item")), "url"))
            if event_id and event_id not in ids:
                ids.append(event_id)
    return ids


def parse_organizer_events(raw: str) -> dict[str, Any] | None:
    """Ids on one page of an organiser's events, and the cursor for the next page."""
    data = _record(parse_json(raw))
    if not data or not isinstance(data.get("events"), list):
        return None
    ids = []
    for item in data["events"]:
        event_id = _text(js.get(_record(item), "id"))
        if event_id and _ID.match(event_id):
            ids.append(event_id)
    pagination = _record(data.get("pagination"))
    continuation = _text(js.get(pagination, "continuation")) if js.get(pagination, "has_more_items") is True else None
    return {"ids": ids, "continuation": continuation}


_DATE = re.compile(r"^([0-9]{4})-([0-9]{2})-([0-9]{2})\Z")
_TIME = re.compile(r"^([0-9]{2}):([0-9]{2})(?::([0-9]{2}))?\Z")


def _local_to_utc(date: Any, time: Any, tz: str) -> str | None:
    """"2026-10-23" + "10:00" in `tz` → ISO UTC, or None."""
    d = _DATE.match(date) if isinstance(date, str) else None
    if not d:
        return None
    t = _TIME.match(time) if isinstance(time, str) else None
    instant = zoned_time_to_utc(
        int(d.group(1)), int(d.group(2)), int(d.group(3)),
        int(t.group(1)) if t else 0, int(t.group(2)) if t else 0, int(t.group(3)) if t and t.group(3) else 0,
        tz=tz,
    )
    return js.iso(instant) if instant is not None else None


def _money(value: Any) -> dict[str, Any] | None:
    price = _record(value)
    amount = js.number(js.get(price, "major_value"))
    currency = _text(js.get(price, "currency"))
    currency = currency.upper() if currency else None
    return {"amount": amount, "currency": currency} if js.finite(amount) and amount >= 0 and currency else None


def _price_range(availability: dict | None) -> dict[str, Any] | None:
    low = _money(js.get(availability, "minimum_ticket_price"))
    high = _money(js.get(availability, "maximum_ticket_price"))
    if not low or not high or low["currency"] != high["currency"] or high["amount"] < low["amount"]:
        return None
    return {"min": low["amount"], "max": high["amount"], "currency": low["currency"]}


_TAG_WEIGHT = {
    "EventbriteCategory": HINT_WEIGHT["eventCategory"],
    "EventbriteSubCategory": HINT_WEIGHT["groupCategory"],
    "EventbriteFormat": HINT_WEIGHT["groupTopic"],
}


def _hints(tags: Any) -> list[dict[str, Any]]:
    """Eventbrite's category, subcategory and format for the event (organisers' own tags are noise)."""
    out = []
    for item in tags if isinstance(tags, list) else []:
        tag = _record(item)
        if not tag:
            continue
        weight = _TAG_WEIGHT.get(js.to_string(tag.get("prefix", js.MISSING)))
        if not weight:
            continue
        label = _text(tag.get("display_name")) or ""
        if label:
            out.append({"label": label, "weight": weight})
    return out


def _round7(n: float) -> float:
    return js.round_half_up(n * 1e7) / 1e7


def _point(address: dict | None) -> dict[str, float] | None:
    raw_lat, raw_lon = js.get(address, "latitude"), js.get(address, "longitude")
    latitude, longitude = js.number(raw_lat), js.number(raw_lon)
    if raw_lat in (None, js.MISSING) or raw_lon in (None, js.MISSING) or not js.finite(latitude) or not js.finite(longitude):
        return None
    if abs(latitude) > 90 or abs(longitude) > 180 or (latitude == 0 and longitude == 0):
        return None
    # Eventbrite sends the same point with float noise from call to call; 7 decimals is ~1 cm.
    return {"latitude": _round7(latitude), "longitude": _round7(longitude)}


def _to_event(raw: dict[str, Any]) -> dict[str, Any]:
    """One event from the details endpoint as an Orbit event, or an error dict."""
    event_id = _text(raw.get("id"))
    title = clamp(one_line(_text(raw.get("name")) or ""), LIMITS["title"])
    if not event_id or not _ID.match(event_id):
        return {"message": "Skipped an Eventbrite event without an id."}
    if js.u16len(title) < 3:
        return {"event": eventbrite_source_id(event_id), "message": "Skipped: no title."}

    tz_text = _text(raw.get("timezone"))
    tz = tz_text if is_valid_timezone(tz_text) else DEFAULT_TIMEZONE
    starts_at = _local_to_utc(raw.get("start_date"), raw.get("start_time"), tz)
    if not starts_at:
        return {"event": eventbrite_source_id(event_id), "message": "Skipped: no start time."}
    ends_at = _local_to_utc(raw.get("end_date"), raw.get("end_time"), tz)

    venue = _record(raw.get("primary_venue"))
    address = _record(js.get(venue, "address"))
    online = raw.get("is_online_event") is True
    street = ", ".join(p for p in (_text(js.get(address, "address_1")), _text(js.get(address, "address_2"))) if p)
    located = _point(address)
    organizer = _text(js.get(_record(raw.get("primary_organizer")), "name"))
    description = html_to_prose(_text(raw.get("summary")))
    series_id = _text(raw.get("series_id"))
    summary = summarise(description)
    if summary is None:
        summary = clamp(f"Hosted by {one_line(organizer)}", LIMITS["summary"]) if organizer else None

    return {
        "source_event_id": eventbrite_source_id(event_id),
        "title": title,
        "summary": summary,
        "description": description,
        "starts_at": starts_at,
        "ends_at": ends_at if ends_at and ends_at > starts_at else None,
        "all_day": False,
        "timezone": tz,
        "format": "online" if online else "in_person",
        "venue_name": None if online else or_null(clamp(one_line(_text(js.get(venue, "name")) or ""), LIMITS["venue"])),
        "address": None if online else or_null(clamp(one_line(street), LIMITS["address"])),
        "city": None if online else or_null(clamp(one_line(_text(js.get(address, "city")) or ""), LIMITS["venue"])),
        "country": None if online else _text(js.get(address, "country")),
        "latitude": None if online or not located else located["latitude"],
        "longitude": None if online or not located else located["longitude"],
        "url": safe_http_url(_text(raw.get("url"))) or f"{EVENTBRITE_ORIGIN}/e/{event_id}",
        "cancelled": raw.get("is_cancelled") is True or raw.get("status") == "canceled",
        "series_key": f"eventbrite-series-{series_id}" if series_id and _ID.match(series_id) else None,
        "series_label": None,
        "interest_hints": _hints(raw.get("tags")),
        "image_url": platform_image("eventbrite", js.get(_record(js.get(_record(raw.get("image")), "original")), "url")),
        "price": _price_range(_record(raw.get("ticket_availability"))),
    }


def parse_eventbrite_events(raw: str) -> FetchedFeed | None:
    """Events from one details response. Bad rows are skipped with an error; nothing throws."""
    data = _record(parse_json(raw))
    if not data or not isinstance(data.get("events"), list):
        return None
    result = FetchedFeed(events=[], errors=[])
    for item in data["events"]:
        event = _record(item)
        # Password-protected or private listings aren't for republishing.
        privacy = event.get("privacy_setting", js.MISSING) if event else None
        if not event or event.get("is_protected_event") is True or (js.truthy(privacy) and privacy != "unlocked"):
            continue
        parsed = _to_event(event)
        if "source_event_id" in parsed:
            result.events.append(parsed)
        else:
            result.errors.append(parsed)
    return result


def parse_eventbrite_description(raw: str) -> str | None:
    """The full description from an event's structured content (its text modules), or None."""
    data = _record(parse_json(raw))
    modules = js.get(data, "modules")
    parts = []
    for item in modules if isinstance(modules, list) else []:
        module = _record(item)
        if js.get(module, "type") != "text":
            continue
        body = _text(js.get(_record(js.get(_record(js.get(module, "data")), "body")), "text"))
        if body:
            parts.append(body)
    return html_to_prose("\n".join(parts))


class EventbriteConnector:
    """Eventbrite organiser, event or search links: the event ids first, then details 50 at a time."""

    async def fetch(self, source: dict[str, Any], now: datetime) -> FetchedFeed:
        link = eventbrite_link(source["url"])
        if not link:
            raise ConnectorError("That isn't an Eventbrite organiser, event or search link.")
        client = PoliteClient()

        truncated = False  # an organiser list (soonest first) cut off at the cap
        if link.type == "event":
            ids = [link.id]
        elif link.type == "search":
            ids = []
            page = 1
            while page <= EVENTBRITE_MAX_PAGES and len(ids) < EVENTBRITE_MAX_EVENTS:
                found = parse_eventbrite_search_page(await client.html(search_page_url(link.url, page)))
                fresh = [event_id for event_id in found if event_id not in ids]
                ids.extend(fresh)
                if not fresh:
                    break  # past the last page, or a search that doesn't page
                page += 1
            if not ids:
                raise ConnectorError("No events found on that Eventbrite search page.")
        else:
            ids = []
            continuation: str | None = None
            page = 0
            while page < EVENTBRITE_MAX_PAGES and len(ids) < EVENTBRITE_MAX_EVENTS:
                listing = parse_organizer_events(await client.json(organizer_events_url(link.id, continuation)))
                if not listing:
                    raise ConnectorError("Couldn't read that organiser's events on Eventbrite.")
                ids.extend([event_id for event_id in listing["ids"] if event_id not in ids])
                continuation = listing["continuation"]
                if not continuation:
                    break
                page += 1
            truncated = bool(continuation) or len(ids) > EVENTBRITE_MAX_EVENTS

        result = FetchedFeed(events=[], errors=[])
        wanted = ids[:EVENTBRITE_MAX_EVENTS]
        for start in range(0, len(wanted), EVENTBRITE_BATCH):
            batch = parse_eventbrite_events(await client.json(event_details_url(wanted[start : start + EVENTBRITE_BATCH])))
            if not batch:
                raise ConnectorError("Couldn't read event details from Eventbrite.")
            result.events.extend(batch.events)
            result.errors.extend(batch.errors)
        if link.type == "event" and not result.events:
            raise ConnectorError("That Eventbrite event isn't public or has ended.")
        # A search is ranked by relevance, so an event dropping out of it says nothing.
        result.coverage = "sample" if link.type == "search" else covered_until(result.events) if truncated else "complete"
        return result
