"""Fatsoma: the JSON:API its own website loads (api.fatsoma.com/v1).

Port of `src/lib/sources/fatsoma.ts` and the Fatsoma connector in `connectors.ts`.

Terms note: Fatsoma's terms (clause 5.1.6) forbid copying or storing its content. The product
owner accepted this exception; keep this source opt-in, capped and easy to disable.
"""

import re
from datetime import datetime
from typing import Any

from . import js, urls
from .base import HINT_WEIGHT, ConnectorError, FetchedFeed, covered_until
from .jsonld import parse_json as _parse_any
from .platforms import UUID_RE, fatsoma_link, platform_image
from .polite import PoliteClient
from .text import LIMITS, clamp, html_to_prose, one_line, or_null, summarise
from .timezone import DEFAULT_TIMEZONE, is_valid_timezone

FATSOMA_API = "https://api.fatsoma.com/v1"
FATSOMA_PAGE_SIZE = 50
FATSOMA_MAX_EVENTS = 250
FATSOMA_MAX_PAGES = 5


def fatsoma_source_id(event_id: str) -> str:
    return f"fatsoma-{event_id}"


def fatsoma_page_lookup_url(vanity: str) -> str:
    """The organiser page with this vanity name (fatsoma.com/p/<vanity>)."""
    return f"{FATSOMA_API}/pages?filter%5Bvanity-name%5D={urls.encode_uri_component(vanity)}"


def fatsoma_events_url(filter_: dict[str, Any], page: int, now: datetime) -> str:
    """One page of active events starting from now, soonest first.

    `filter_` is one of {"page_id": …}, {"vanity": …} or a Discover search
    {"lat_lon": (lat, lon) | None, "category_id": str | None}.
    """
    url = urls.parse(f"{FATSOMA_API}/events")
    assert url is not None
    params = url.search_params
    if "page_id" in filter_:
        params.set("filter[page.id]", filter_["page_id"])
    if "vanity" in filter_:
        params.set("filter[vanity-name]", filter_["vanity"])
    if filter_.get("lat_lon"):
        params.set("filter[location.lat-lon]", ",".join(js.num_str(n) for n in filter_["lat_lon"]))
    if filter_.get("category_id"):
        params.set("filter[categories.id]", filter_["category_id"])
    params.set("filter[status]", "active")
    params.set("filter[starts-at][gte]", js.iso(js.to_ms(now))[:19])
    params.set("include", "location,page,categories")
    # `id` breaks ties: with the start time alone, events starting together shift between pages.
    params.set("sort", "starts-at,id")
    params.set("page[size]", str(FATSOMA_PAGE_SIZE))
    params.set("page[number]", str(page))
    return url.href


def _record(value: Any) -> dict | None:
    return value if isinstance(value, dict) else None


def _text(value: Any) -> str | None:
    return js.trim(value) if isinstance(value, str) and js.trim(value) else None


def _parse_json(raw: str) -> dict | None:
    return _record(_parse_any(raw))


_CITY_POINT = re.compile(r"filter\[location\.lat-lon\]=(-?[0-9]{1,3}(?:\.[0-9]+)?),(-?[0-9]{1,3}(?:\.[0-9]+)?)")


def parse_fatsoma_city_page(html: str) -> tuple[float, float] | None:
    """The search coordinates a city page (/l/gb/london) uses for its own event list."""
    match = _CITY_POINT.search(html)
    if not match:
        return None
    lat, lon = float(match.group(1)), float(match.group(2))
    return (lat, lon) if abs(lat) <= 90 and abs(lon) <= 180 else None


def parse_fatsoma_page_lookup(raw: str) -> str | None:
    """The id of the organiser page from a vanity-name lookup, or None when there is none."""
    data = js.get(_parse_json(raw), "data")
    first = _record((data[0] if data else js.MISSING) if isinstance(data, list) else data)
    page_id = _text(js.get(first, "id"))
    return page_id if page_id and UUID_RE.match(page_id) else None


def _price_range(attributes: dict) -> dict[str, Any] | None:
    """Pence (Fatsoma's unit) → a price range in pounds, or None when it doesn't add up."""
    raw_min, raw_max = attributes.get("price-min", js.MISSING), attributes.get("price-max", js.MISSING)
    low, high = js.number(raw_min), js.number(raw_max)
    currency = _text(attributes.get("currency"))
    currency = currency.upper() if currency else None
    if raw_min in (None, js.MISSING) or raw_max in (None, js.MISSING) or not currency:
        return None
    if not js.finite(low) or not js.finite(high) or low < 0 or high < low:
        return None
    return {"min": low / 100, "max": high / 100, "currency": currency}


def _point(location: Any) -> dict[str, float] | None:
    raw_lat, raw_lon = js.get(location, "latitude"), js.get(location, "longitude")
    latitude, longitude = js.number(raw_lat), js.number(raw_lon)
    if raw_lat in (None, js.MISSING) or raw_lon in (None, js.MISSING) or not js.finite(latitude) or not js.finite(longitude):
        return None
    if abs(latitude) > 90 or abs(longitude) > 180 or (latitude == 0 and longitude == 0):
        return None
    return {"latitude": latitude, "longitude": longitude}


def _related(resource: dict, name: str) -> list[dict]:
    data = js.get(js.get(resource.get("relationships", js.MISSING), name), "data")
    items = data if isinstance(data, list) else [data] if js.truthy(data) else []
    return [item for item in items if isinstance(item, dict)]


_VANITY = re.compile(r"^[a-z0-9]+$", re.I | re.A)
_SEO = re.compile(r"^[a-z0-9-]+$", re.I | re.A)


def _to_event(resource: dict, lookup: Any) -> dict[str, Any]:
    event_id = _text(resource.get("id"))
    attributes = resource.get("attributes")
    a = attributes if isinstance(attributes, dict) else {}
    if not event_id or not UUID_RE.match(event_id):
        return {"message": "Skipped a Fatsoma event without an id."}
    title = clamp(one_line(_text(a.get("name")) or ""), LIMITS["title"])
    vanity = _text(a.get("vanity-name"))
    starts_at = js.iso_or_none(a.get("starts-at"))
    if js.u16len(title) < 3:
        return {"event": fatsoma_source_id(event_id), "message": "Skipped: no title."}
    if not starts_at:
        return {"event": fatsoma_source_id(event_id), "message": "Skipped: no start time."}
    if not vanity or not _VANITY.match(vanity):
        return {"event": fatsoma_source_id(event_id), "message": "Skipped: no event page."}

    ends_at = js.iso_or_none(a.get("ends-at"))
    location_refs = _related(resource, "location")
    location = lookup("locations", location_refs[0].get("id", js.MISSING)) if location_refs else None
    page_refs = _related(resource, "page")
    organiser = _text(js.get(lookup("pages", page_refs[0].get("id", js.MISSING)), "name")) if page_refs else None
    located = _point(location)
    tz = _text(js.get(location, "timezone"))
    seo = _text(a.get("seo-name"))
    description = html_to_prose(_text(a.get("description")))
    # `address` is the full one-line address (with postcode); the line fields are usually empty.
    street = _text(js.get(location, "address"))
    if street is None:
        street = ", ".join(
            p for p in (
                _text(js.get(location, "address-line-1")),
                _text(js.get(location, "address-line-2")),
                _text(js.get(location, "postal-code")),
            ) if p
        )
    hints = []
    for ref in _related(resource, "categories"):
        name = _text(js.get(lookup("categories", ref.get("id", js.MISSING)), "name"))
        if name:
            hints.append({"label": name, "weight": HINT_WEIGHT["eventCategory"]})
    summary = summarise(description)
    if summary is None:
        summary = clamp(f"Hosted by {one_line(organiser)}", LIMITS["summary"]) if organiser else None

    return {
        "source_event_id": fatsoma_source_id(event_id),
        "title": title,
        "summary": summary,
        "description": description,
        "starts_at": starts_at,
        "ends_at": ends_at if ends_at and ends_at > starts_at else None,
        "all_day": False,
        "timezone": tz if tz and is_valid_timezone(tz) else DEFAULT_TIMEZONE,
        "format": "in_person",
        "venue_name": or_null(clamp(one_line(_text(js.get(location, "name")) or ""), LIMITS["venue"])),
        "address": or_null(clamp(one_line(street), LIMITS["address"])),
        "city": or_null(clamp(one_line(_text(js.get(location, "city")) or ""), LIMITS["venue"])),
        "country": _text(js.get(location, "country")),
        "latitude": located["latitude"] if located else None,
        "longitude": located["longitude"] if located else None,
        "url": f"https://www.fatsoma.com/e/{vanity}" + (f"/{seo}" if seo and _SEO.match(seo) else ""),
        "cancelled": a.get("health") == "cancelled",
        "series_key": None,
        "series_label": None,
        "interest_hints": hints,
        "image_url": platform_image("fatsoma", a.get("asset-url")),
        "price": _price_range(a),
    }


def parse_fatsoma_events(raw: str) -> dict[str, Any] | None:
    """Events from one API page: {"events", "errors", "total"}. Bad rows are skipped, not thrown."""
    data = _parse_json(raw)
    if not data or not isinstance(data.get("data"), list):
        return None
    included: dict[str, Any] = {}
    for item in data.get("included") if isinstance(data.get("included"), list) else []:
        resource = _record(item)
        if (
            resource
            and isinstance(resource.get("type"), str)
            and isinstance(resource.get("id"), str)
            and js.truthy(resource.get("attributes", js.MISSING))
        ):
            included[f"{resource['type']}:{resource['id']}"] = resource["attributes"]

    def lookup(kind: str, ref: Any) -> Any:
        return included.get(f"{kind}:{ref}") if isinstance(ref, str) else None

    events: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for item in data["data"]:
        resource = _record(item)
        if not resource or resource.get("type") != "events":
            continue
        parsed = _to_event(resource, lookup)
        if "source_event_id" in parsed:
            events.append(parsed)
        else:
            errors.append(parsed)
    total = js.number(js.get(_record(data.get("meta")), "total-count"))
    return {"events": events, "errors": errors, "total": total if js.finite(total) else None}


class FatsomaConnector:
    """Fatsoma organiser, event, city or Discover links, through the API Fatsoma's own site uses."""

    async def fetch(self, source: dict[str, Any], now: datetime) -> FetchedFeed:
        link = fatsoma_link(source["url"])
        if not link:
            raise ConnectorError("That isn't a Fatsoma organiser, event, city or Discover link.")
        client = PoliteClient()

        if link.type == "event":
            filter_: dict[str, Any] = {"vanity": link.vanity}
        elif link.type == "page":
            page_id = parse_fatsoma_page_lookup(await client.json(fatsoma_page_lookup_url(link.vanity or "")))
            if not page_id:
                raise ConnectorError(f'No Fatsoma organiser page called "{link.vanity}".')
            filter_ = {"page_id": page_id}
        elif link.type == "discover":
            filter_ = {"lat_lon": link.lat_lon, "category_id": link.category_id}
        else:
            lat_lon = parse_fatsoma_city_page(await client.html(link.url))
            if not lat_lon:
                raise ConnectorError("Couldn't find the event list on that Fatsoma city page.")
            filter_ = {"lat_lon": lat_lon}

        result = FetchedFeed(events=[], errors=[])
        truncated = True  # until a page shows it was the last one
        page = 1
        while page <= FATSOMA_MAX_PAGES and len(result.events) < FATSOMA_MAX_EVENTS:
            response = parse_fatsoma_events(await client.json(fatsoma_events_url(filter_, page, now)))
            if not response:
                raise ConnectorError("Couldn't read events from Fatsoma.")
            seen = {event["source_event_id"] for event in result.events}
            result.events.extend(event for event in response["events"] if event["source_event_id"] not in seen)
            result.errors.extend(response["errors"])
            read = len(response["events"]) + len(response["errors"])
            total = response["total"]
            if read < FATSOMA_PAGE_SIZE or (total is not None and page * FATSOMA_PAGE_SIZE >= total):
                truncated = False
                break
            page += 1
        result.events = result.events[:FATSOMA_MAX_EVENTS]
        result.coverage = covered_until(result.events) if truncated else "complete"
        if link.type == "event" and not result.events:
            raise ConnectorError("That Fatsoma event isn't on sale or has ended.")
        return result
