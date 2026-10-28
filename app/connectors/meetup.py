"""Meetup: search pages through Meetup's own GraphQL, series dates, and event pages.

Port of `meetup-search.ts`, `meetup-series.ts`, `meetup-page.ts` and the meetup-search
connector in `connectors.ts`.

Robots note: Meetup's robots.txt disallows the `source` and `categoryId` find-page
parameters. The product owner accepted this exception; keep the source opt-in, capped and
easy to disable.
"""

import re
from datetime import datetime
from typing import Any, Awaitable, Callable

from . import fetch as http
from . import js
from .base import HINT_WEIGHT, FetchedFeed, error_message
from .jsonld import address_country, first_json_ld_event, parse_json
from .platforms import MEETUP_LOCATION_ERROR, meetup_find_missing_location
from .polite import fetch_html
from .text import LIMITS, clamp, clean_prose, decode_entities, one_line, or_null, safe_http_url, strip_html, summarise
from .timezone import is_valid_timezone

MEETUP_SEARCH_GQL = "https://www.meetup.com/gql2"
MEETUP_SEARCH_PAGE_SIZE = 50
MEETUP_SEARCH_MAX_PAGES = 6
MEETUP_SEARCH_MAX_EVENTS = 250
MEETUP_SERIES_MAX_DATES = 12  # about three months of a weekly meetup
MEETUP_SERIES_MAX_PER_RUN = 25  # GraphQL lookups per source run
DAY_MS = 24 * 60 * 60 * 1000

_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>([\s\S]*?)</script>')
_NEXT_DATA_LOOSE = re.compile(r"<script[^>]*id=[\"']__NEXT_DATA__[\"'][^>]*>([\s\S]*?)</script>", re.I | re.A)


def _id(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return js.num_str(value)
    return None


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


# ─── Find page ────────────────────────────────────────────────────────────────

def parse_meetup_find_page(html: str) -> dict[str, Any] | None:
    """Reads the find page's own SSR state: coordinates, filters and first cursor."""
    match = _NEXT_DATA.search(html)
    raw = match.group(1) if match else None
    if not raw:
        return None
    data = parse_json(raw)
    if data is js.MISSING:
        return None
    root = js.get(js.get(js.get(js.get(data, "props"), "pageProps"), "__APOLLO_STATE__"), "ROOT_QUERY")
    if not js.truthy(root) or not isinstance(root, dict):
        return None
    key = next((name for name in root if name.startswith("recommendedEvents:")), None)
    if key is None:
        return None
    args = parse_json(key[len("recommendedEvents:") :])
    if args is js.MISSING:
        return None
    raw_filter = js.get(args, "filter")
    flt = raw_filter if js.truthy(raw_filter) and raw_filter is not None else {}
    lat = js.number(js.get(flt, "lat"))
    lon = js.number(js.get(flt, "lon"))
    if not js.finite(lat) or not js.finite(lon):
        return None
    connection = root[key]
    page_info = js.get(connection, "pageInfo")
    end_cursor = js.get(page_info, "endCursor")
    start_range = js.get(flt, "startDateRange")
    index_alias = js.get(flt, "indexAlias")
    return {
        "lat": lat,
        "lon": lon,
        "start_date_range": start_range if isinstance(start_range, str) else None,
        "index_alias": index_alias if isinstance(index_alias, str) else None,
        "category_id": _id(js.get(flt, "categoryId")),
        "topic_category_id": _id(js.get(flt, "topicCategoryId")),
        "end_cursor": None if end_cursor is js.MISSING else end_cursor,
        "has_next_page": js.truthy(js.get(page_info, "hasNextPage")),
    }


MEETUP_SEARCH_QUERY = """query meetupSearch(
  $lat: Float!, $lon: Float!, $categoryId: ID, $topicCategoryId: ID,
  $startDateRange: String, $first: Int, $after: String,
  $sortField: RecommendedEventsSortField, $indexAlias: String,
  $doConsolidateEvents: Boolean, $doPromotePaypalEvents: Boolean
) {
  result: recommendedEvents(
    filter: {
      lat: $lat, lon: $lon, categoryId: $categoryId, topicCategoryId: $topicCategoryId,
      startDateRange: $startDateRange, doConsolidateEvents: $doConsolidateEvents,
      doPromotePaypalEvents: $doPromotePaypalEvents, indexAlias: $indexAlias
    }
    first: $first, after: $after, sort: { sortField: $sortField }
  ) {
    pageInfo { hasNextPage endCursor }
    totalCount
    edges { node { ...SearchEvent } }
  }
}
fragment SearchEvent on Event {
  id title dateTime description eventType eventUrl
  featuredEventPhoto { highResUrl }
  series { description }
  group { name urlname timezone topicCategory { name } }
  venue { name address city state country }
}"""


def _event_format(value: Any) -> str:
    if value == "ONLINE":
        return "online"
    if value == "HYBRID":
        return "hybrid"
    return "in_person"


def parse_meetup_search_response(payload: Any) -> dict[str, Any]:
    """Maps one GraphQL page into Orbit's normalised events."""
    result = js.get(js.get(payload, "data"), "result")
    page_info = js.get(result, "pageInfo")
    end_cursor = js.get(page_info, "endCursor")
    out: dict[str, Any] = {
        "events": [],
        "errors": [],
        "has_next_page": js.truthy(js.get(page_info, "hasNextPage")),
        "next_cursor": None if end_cursor in (js.MISSING, None) else end_cursor,
    }
    if not js.truthy(result):
        errors = js.get(payload, "errors")
        first = errors[0] if isinstance(errors, list) and errors else js.MISSING
        message = js.get(first, "message")
        out["errors"].append({"message": message if message not in (js.MISSING, None) else "Meetup search returned no results."})
        return out

    edges = js.get(result, "edges")
    for edge in edges if isinstance(edges, list) else []:
        node = js.get(edge, "node")
        if not js.truthy(node):
            continue
        id_value = _id(js.get(node, "id"))
        title = clamp(one_line(_str(js.get(node, "title"))), LIMITS["title"])
        date_time = js.get(node, "dateTime")
        starts = js.parse_date(date_time) if isinstance(date_time, str) else None
        event_url = js.get(node, "eventUrl")
        url = safe_http_url(event_url if isinstance(event_url, str) else None)
        if not id_value or js.u16len(title) < 3 or starts is None or not url:
            error: dict[str, Any] = {"message": "Skipped: missing id, title, start or event URL"}
            if id_value:
                error = {"event": id_value, **error}
            out["errors"].append(error)
            continue

        description = or_null(
            clamp(clean_prose(strip_html(_str(js.get(node, "description"))), keep_emphasis=True), LIMITS["description"])
        )
        group = js.get(node, "group")
        group_name = one_line(_str(js.get(group, "name")))
        group_tz = js.get(group, "timezone")
        timezone = group_tz if isinstance(group_tz, str) and is_valid_timezone(group_tz) else "Europe/London"
        venue = js.get(node, "venue")
        series_description = js.get(js.get(node, "series"), "description")
        topic = js.get(js.get(group, "topicCategory"), "name")
        photo = js.get(js.get(node, "featuredEventPhoto"), "highResUrl")
        country = js.get(venue, "country")
        out["events"].append({
            "source_event_id": f"event_{id_value}@meetup.com",
            "title": title,
            "summary": summarise(description) or (clamp(group_name, LIMITS["summary"]) if group_name else None),
            "description": description,
            "starts_at": js.iso(starts),
            "ends_at": None,
            "all_day": False,
            "timezone": timezone,
            "format": _event_format(js.get(node, "eventType")),
            "venue_name": or_null(clamp(one_line(_str(js.get(venue, "name"))), LIMITS["venue"])),
            "address": or_null(clamp(one_line(_str(js.get(venue, "address"))), LIMITS["address"])),
            "city": or_null(clamp(one_line(_str(js.get(venue, "city"))), LIMITS["venue"])),
            "country": country if isinstance(country, str) else None,
            "latitude": None,
            "longitude": None,
            "url": url,
            "cancelled": False,
            # Search consolidates a series into one result; the connector expands it to every date.
            "series_label": (
                or_null(clamp(one_line(series_description), LIMITS["summary"]))
                if isinstance(series_description, str) else None
            ),
            # The group's category; its topics come from the event page when it's read.
            "interest_hints": [{"label": topic, "weight": HINT_WEIGHT["groupCategory"]}] if isinstance(topic, str) else [],
            "image_url": meetup_image(photo if isinstance(photo, str) else None),
        })
    return out


def _gql_headers(referer: str) -> dict[str, str]:
    return {
        "content-type": "application/json",
        "accept": "application/json",
        "user-agent": http.USER_AGENT,
        "origin": "https://www.meetup.com",
        "referer": referer,
    }


async def fetch_meetup_search_page(args: dict[str, Any], after: str | None, referer: str) -> dict[str, Any]:
    """One GraphQL page. Raises on HTTP errors so the source run records them."""
    variables = {
        "lat": args["lat"],
        "lon": args["lon"],
        "categoryId": args["category_id"],
        "topicCategoryId": args["topic_category_id"],
        "startDateRange": args["start_date_range"],
        "first": MEETUP_SEARCH_PAGE_SIZE,
        "after": after,
        "sortField": "RELEVANCE",
        "indexAlias": args["index_alias"],
        "doConsolidateEvents": True,
        "doPromotePaypalEvents": False,
    }
    payload = {"operationName": "meetupSearch", "variables": variables, "query": MEETUP_SEARCH_QUERY}
    status, body = await http.post_json(MEETUP_SEARCH_GQL, payload, _gql_headers(referer), timeout=20.0)
    if status in (429, 503):
        raise http.FetchHttpError(f"Meetup search asked us to slow down (HTTP {status}).", status)
    if not 200 <= status < 300:
        raise http.FetchHttpError(f"Meetup search returned HTTP {status}.", status)
    if not js.truthy(body):
        raise Exception("Meetup search returned an invalid response.")
    return parse_meetup_search_response(body)


# ─── Series ───────────────────────────────────────────────────────────────────

SERIES_QUERY = """query eventSeries($id: ID!) {
  event(id: $id) {
    series {
      description
      events { edges { node { id dateTime endTime eventUrl status featuredEventPhoto { highResUrl } } } }
    }
  }
}"""

_SERIES_ID = re.compile(r"^[a-z0-9]+$", re.I | re.A)


def parse_meetup_series_response(payload: Any) -> dict[str, Any] | None:
    """The series' label and dates, or None when the event isn't part of a series."""
    series = js.get(js.get(js.get(payload, "data"), "event"), "series")
    if not js.truthy(series):
        return None
    dates = []
    edges = js.get(js.get(series, "events"), "edges")
    for edge in edges if isinstance(edges, list) else []:
        node = js.get(edge, "node")
        id_value = _id(js.get(node, "id"))
        starts_at = js.iso_or_none(js.get(node, "dateTime"))
        event_url = js.get(node, "eventUrl")
        url = safe_http_url(event_url if isinstance(event_url, str) else None)
        if not id_value or not _SERIES_ID.match(id_value) or not starts_at or not url:
            continue
        photo = js.get(js.get(node, "featuredEventPhoto"), "highResUrl")
        dates.append({
            "id": id_value,
            "starts_at": starts_at,
            "ends_at": js.iso_or_none(js.get(node, "endTime")),
            "url": url,
            "cancelled": js.get(node, "status") == "CANCELLED",
            "image_url": meetup_image(photo if isinstance(photo, str) else None),
        })
    description = js.get(series, "description")
    label = or_null(clamp(one_line(description), LIMITS["summary"])) if isinstance(description, str) else None
    return {"label": label, "dates": dates}


async def fetch_meetup_series(event_id: str, referer: str) -> dict[str, Any] | None:
    """One GraphQL call for an event's series. Raises on HTTP errors so the run records them."""
    payload = {"operationName": "eventSeries", "variables": {"id": event_id}, "query": SERIES_QUERY}
    status, body = await http.post_json(MEETUP_SEARCH_GQL, payload, _gql_headers(referer), timeout=20.0)
    if not 200 <= status < 300:
        raise http.FetchHttpError(f"Meetup series lookup returned HTTP {status}.", status)
    return parse_meetup_series_response(body)


_EVENT_ID = re.compile(r"^event_([a-z0-9]+)@meetup\.com$", re.I | re.A)
_GROUP = re.compile(r"meetup\.com/([^/]+)/events/", re.I | re.A)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def meetup_event_id(source_event_id: str) -> str | None:
    """Meetup's event id from Orbit's source id (`event_<id>@meetup.com`)."""
    match = _EVENT_ID.match(source_event_id)
    return match.group(1) if match else None


def meetup_series_key(event: dict[str, Any]) -> str | None:
    """Shared by every date of one series: the group plus the title (`meetup:<group>:<title>`)."""
    match = _GROUP.search(event.get("url") or "")
    group = match.group(1).lower() if match else None
    title = js.trim(_NON_ALNUM.sub(" ", event["title"].lower()))
    return f"meetup:{group}:{title}" if group and title else None


def expand_series(base: dict[str, Any], series: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    """One event per upcoming date of the series, soonest first (dates over a day old dropped)."""
    series_key = meetup_series_key(base)
    series_label = series["label"] or base.get("series_label") or None
    cutoff = js.to_ms(now) - DAY_MS
    dates = [d for d in series["dates"] if (js.parse_date(d["ends_at"] or d["starts_at"]) or 0) >= cutoff]
    dates.sort(key=lambda d: d["starts_at"])
    dates = dates[:MEETUP_SERIES_MAX_DATES]
    if not dates:
        return [{**base, "series_key": series_key, "series_label": series_label}]
    return [
        {
            **base,
            "source_event_id": f"event_{d['id']}@meetup.com",
            "starts_at": d["starts_at"],
            "ends_at": d["ends_at"],
            "url": d["url"],
            "cancelled": d["cancelled"],
            "image_url": d.get("image_url") or base.get("image_url") or None,
            "series_key": series_key,
            "series_label": series_label,
        }
        for d in dates
    ]


SeriesLookup = Callable[[str, str], Awaitable[dict[str, Any] | None]]


async def with_series_dates(
    events: list[dict[str, Any]],
    *,
    now: datetime,
    referer: str,
    errors: list[dict[str, Any]],
    lookup: SeriesLookup | None = None,
    pause_ms: int = 500,
) -> list[dict[str, Any]]:
    """Replaces each series event (one with a `series_label`) by all of its upcoming dates."""
    lookup = lookup or fetch_meetup_series
    out: list[dict[str, Any]] = []
    expanded: set[str] = set()
    lookups = 0
    for event in events:
        key = meetup_series_key(event) if event.get("series_label") else None
        event_id = meetup_event_id(event["source_event_id"])
        if not key or not event_id:
            out.append(event)
            continue
        if key in expanded:
            continue  # another date of a series already expanded
        if lookups >= MEETUP_SERIES_MAX_PER_RUN:
            out.append({**event, "series_key": key})
            continue
        if lookups > 0 and pause_ms > 0:
            await http.sleep(pause_ms)
        lookups += 1
        try:
            series = await lookup(event_id, referer)
            out.extend(expand_series(event, series, now) if series else [{**event, "series_key": key}])
            expanded.add(key)
        except Exception as exc:  # noqa: BLE001 - keep the single date, record why
            errors.append({"event": event["source_event_id"], "message": f"Series dates: {error_message(exc)}"})
            out.append({**event, "series_key": key})
    # Search results may also list later dates of an expanded series: keep one copy of each date.
    seen: set[str] = set()
    unique = []
    for event in out:
        if event["source_event_id"] in seen:
            continue
        seen.add(event["source_event_id"])
        unique.append(event)
    return unique


# ─── Event pages ──────────────────────────────────────────────────────────────

IMAGE_HOSTS = {"secure.meetupstatic.com", "secure-content.meetupstatic.com"}


def _safe_image(value: str | None) -> str | None:
    if not value:
        return None
    from . import urls

    url = urls.parse(decode_entities(value))
    return url.href if url is not None and url.protocol == "https:" and url.hostname in IMAGE_HOSTS else None


def meetup_image(value: str | None) -> str | None:
    """A Meetup-hosted image URL (https on Meetup's image hosts), or None."""
    return _safe_image(value)


def is_meetup_hosted_image(value: str | None) -> bool:
    """True for a cover still hotlinked from Meetup (not yet copied into Orbit's Cloudinary)."""
    if not value:
        return False
    from . import urls

    url = urls.parse(value)
    return url is not None and url.hostname in IMAGE_HOSTS


_FEATURED = re.compile(r'"featuredEventPhoto":\{[^}]*?"source":"([^"]+)"')
_OG_TAG = re.compile(r"<meta[^>]+property=[\"']og:image[\"'][^>]*>", re.I | re.A)
_CONTENT = re.compile(r"content=[\"']([^\"']+)[\"']", re.I | re.A)


def _cover_image(html: str) -> str | None:
    """The event's own photo at full size, else og:image."""
    featured = _FEATURED.search(html)
    og = _OG_TAG.search(html)
    content = _CONTENT.search(og.group(0)) if og else None
    return _safe_image(featured.group(1) if featured else None) or _safe_image(content.group(1) if content else None)


def _text(value: Any) -> str | None:
    return js.trim(value) if isinstance(value, str) and js.trim(value) else None


def _number(value: str | None) -> float | None:
    if not value:
        return None
    n = js.number(value)
    return n if js.finite(n) else None


def _parse_venue_object(obj: str) -> dict[str, Any]:
    def string(key: str) -> str | None:
        match = re.search(f'"{key}":{js.S}*"([^"]*)"', obj)
        return match.group(1) if match else None

    def numeric(key: str) -> float | None:
        match = re.search(f'"{key}":{js.S}*(-?[0-9]+(?:\\.[0-9]+)?)', obj)
        return _number(match.group(1) if match else None)

    lng = numeric("lng")
    return {
        "name": string("name"),
        "address": string("address"),
        "city": string("city"),
        "country": string("country") or None,
        "latitude": numeric("lat"),
        "longitude": lng if lng is not None else numeric("lon"),
    }


ONLINE_VENUE = "Online event"  # Meetup's placeholder venue on online events
_LAT_LNG_PAIR = re.compile(
    f'"lat":{js.S}*(-?[0-9]+(?:\\.[0-9]+)?)[^}}]{{0,100}}?"(?:lng|lon)":{js.S}*(-?[0-9]+(?:\\.[0-9]+)?)'
)


def _find_venue_state(html: str, name: str | None) -> dict[str, Any] | None:
    fallback = None
    for match in re.finditer(r'"venue":\{', html):
        start = match.start()
        end = html.find("}", start)
        if end < 0:
            continue
        state = _parse_venue_object(html[start:end])
        if fallback is None:
            fallback = state
        if name and state["name"] == name:
            return state
    if fallback is not None:
        return fallback
    # Last resort: the page may keep the coordinates outside the venue object.
    pair = _LAT_LNG_PAIR.search(html)
    if not pair:
        return None
    return {
        "name": None, "address": None, "city": None, "country": None,
        "latitude": _number(pair.group(1)), "longitude": _number(pair.group(2)),
    }


def _venue_state(html: str, name: str | None) -> dict[str, Any] | None:
    state = _find_venue_state(html, name)
    # Online events carry a placeholder venue ("Online event", a point in the Pacific): not a place.
    return None if state is not None and state["name"] == ONLINE_VENUE else state


def _page_event(html: str) -> dict[str, Any] | None:
    """The event record in the page's __NEXT_DATA__ (`props.pageProps.event`)."""
    match = _NEXT_DATA_LOOSE.search(html)
    raw = match.group(1) if match else None
    if not raw:
        return None
    data = parse_json(raw)
    if data is js.MISSING:
        return None
    event = js.get(js.get(js.get(data, "props"), "pageProps"), "event")
    return event if isinstance(event, dict) else None


def _page_description(page: dict[str, Any] | None) -> str | None:
    description = js.get(page, "description")
    return description if isinstance(description, str) and js.trim(description) else None


def _page_hints(page: dict[str, Any] | None) -> list[dict[str, Any]]:
    group = js.get(page, "group")
    hints = []
    topic = js.get(js.get(group, "topicCategory"), "name")
    if isinstance(topic, str):
        hints.append({"label": topic, "weight": HINT_WEIGHT["groupCategory"]})
    topics = js.get(group, "topics")
    for item in topics if isinstance(topics, list) else []:
        name = js.get(item, "name")
        if isinstance(name, str):
            hints.append({"label": name, "weight": HINT_WEIGHT["groupTopic"]})
    return hints


_CURRENCY = re.compile(r"^[A-Za-z]{3}$")


def page_price(page: dict[str, Any] | None) -> dict[str, Any] | None:
    """The event's fee: {amount: 0} when free (`feeSettings` null); None when the page doesn't say."""
    if page is None or "feeSettings" not in page:
        return None
    fee = page["feeSettings"]
    if fee is None:
        return {"amount": 0, "currency": None}
    amount_value = js.get(fee, "amount")
    amount = amount_value if isinstance(amount_value, (int, float)) and not isinstance(amount_value, bool) else None
    currency_value = js.get(fee, "currency")
    currency = currency_value.upper() if isinstance(currency_value, str) and _CURRENCY.match(currency_value) else None
    if amount is None or not js.finite(amount) or amount < 0 or (amount > 0 and not currency):
        return None
    return {"amount": amount, "currency": currency}


def _page_series_label(page: dict[str, Any] | None) -> str | None:
    description = js.get(js.get(page, "series"), "description")
    return or_null(clamp(one_line(description), LIMITS["summary"])) if isinstance(description, str) else None


def _valid_point(latitude: float | None, longitude: float | None) -> tuple[float, float] | None:
    if latitude is None or longitude is None:
        return None
    if abs(latitude) > 90 or abs(longitude) > 180:
        return None
    return latitude, longitude


_ID_PATTERN = re.compile(r"/events/([0-9]+|[a-z]{10,})(?:/|$)")


def _location(event: dict[str, Any]) -> tuple[dict | None, dict | None]:
    location = event.get("location")
    location = location if js.is_object(location) else None
    address = js.get(location, "address")
    address = address if js.is_object(address) else None
    return (location if isinstance(location, dict) else ({} if location is not None else None),
            address if isinstance(address, dict) else ({} if address is not None else None))


def parse_meetup_linked_event(html: str, page_url: str) -> dict[str, Any] | None:
    """One Meetup event page as an Orbit event (an admin pasted a single event link)."""
    event = first_json_ld_event(html)
    if event is None:
        return None
    # Unannounced dates of a series have a lowercase-letter id until Meetup gives them a number.
    event_url = event.get("url")
    id_match = _ID_PATTERN.search(page_url) or (_ID_PATTERN.search(event_url) if isinstance(event_url, str) else None)
    event_id = id_match.group(1) if id_match else None
    title = clamp(one_line(_text(event.get("name")) or ""), LIMITS["title"])
    start_date = event.get("startDate")
    starts = js.parse_date(start_date) if isinstance(start_date, str) else None
    end_date = event.get("endDate")
    ends = js.parse_date(end_date) if isinstance(end_date, str) and end_date else None
    url = safe_http_url(_text(event_url) or page_url) or safe_http_url(page_url)
    if not event_id or js.u16len(title) < 3 or starts is None or not url:
        return None

    location, address = _location(event)
    venue_name = _text(js.get(location, "name"))
    state = _venue_state(html, venue_name)
    point = _valid_point(state["latitude"] if state else None, state["longitude"] if state else None)
    tz_match = re.search(r'"timezone":"([^"]+)"', html)
    timezone = tz_match.group(1) if tz_match else None

    organizer = event.get("organizer")
    if isinstance(organizer, list):
        organizer = organizer[0] if organizer else js.MISSING
    organizer_name = _text(js.get(organizer, "name")) if js.is_object(organizer) else None
    page = _page_event(html)
    description = or_null(clamp(
        clean_prose(strip_html(_page_description(page) or _text(event.get("description")) or ""), keep_emphasis=True),
        LIMITS["description"],
    ))
    mode = _text(event.get("eventAttendanceMode")) or ""
    status = _text(event.get("eventStatus")) or ""
    state_name = state["name"] if state else None
    state_address = state["address"] if state else None
    state_city = state["city"] if state else None
    state_country = state["country"] if state else None

    return {
        "source_event_id": f"event_{event_id}@meetup.com",
        "title": title,
        "summary": summarise(description) or (clamp(organizer_name, LIMITS["summary"]) if organizer_name else None),
        "description": description,
        "starts_at": js.iso(starts),
        "ends_at": js.iso(ends) if ends is not None else None,
        "all_day": False,
        "timezone": timezone if timezone and is_valid_timezone(timezone) else "Europe/London",
        "format": "online" if "Online" in mode else "hybrid" if "Mixed" in mode else "in_person",
        "venue_name": or_null(clamp(_coalesce(state_name, venue_name) or "", LIMITS["venue"])),
        "address": or_null(clamp(_coalesce(state_address, _text(js.get(address, "streetAddress"))) or "", LIMITS["address"])),
        "city": or_null(clamp(_coalesce(state_city, _text(js.get(address, "addressLocality"))) or "", LIMITS["venue"])),
        "country": _coalesce(state_country, address_country(address)),
        "latitude": point[0] if point else None,
        "longitude": point[1] if point else None,
        "url": url,
        "cancelled": "Cancelled" in status,
        "series_label": _page_series_label(page),
        "interest_hints": _page_hints(page),
        "image_url": _cover_image(html),
    }


def _coalesce(*values: Any) -> Any:
    """`a ?? b`: the first value that isn't None."""
    for value in values:
        if value is not None:
            return value
    return None


def parse_meetup_event_page(html: str) -> dict[str, Any] | None:
    """Venue details, cover and price from a Meetup event page, or None when it has none of them."""
    event = first_json_ld_event(html)
    location = event.get("location") if event is not None else None
    location = location if js.is_object(location) else None
    address = js.get(location, "address") if location is not None else None
    address = address if js.is_object(address) else None
    location_d = location if isinstance(location, dict) else {}
    address_d = address if isinstance(address, dict) else ({} if address is not None else None)

    json_ld_name = _text(js.get(location_d, "name"))
    state = _venue_state(html, json_ld_name)
    venue_name = _coalesce(json_ld_name, state["name"] if state else None)
    street = _coalesce(state["address"] if state else None, _text(js.get(address_d, "streetAddress")))
    city = _coalesce(state["city"] if state else None, _text(js.get(address_d, "addressLocality")))
    country = _coalesce(state["country"] if state else None, address_country(address_d))
    point = _valid_point(state["latitude"] if state else None, state["longitude"] if state else None)
    cover_url = _cover_image(html)
    page = _page_event(html)
    hints = _page_hints(page)
    price = page_price(page)

    if not venue_name and not street and not city and not point and not cover_url and not price and not hints:
        return None
    return {
        "venue_name": or_null(clamp(decode_entities(venue_name or ""), LIMITS["venue"])),
        "address": or_null(clamp(decode_entities(street or ""), LIMITS["address"])),
        "city": or_null(clamp(decode_entities(city or ""), LIMITS["venue"])),
        "country": country,
        "latitude": point[0] if point else None,
        "longitude": point[1] if point else None,
        "cover_url": cover_url,
        "price": price,
        "interest_hints": hints,
    }


# ─── Connector ────────────────────────────────────────────────────────────────

class MeetupSearchConnector:
    """Meetup's find page + internal GraphQL, capped and de-duplicated."""

    async def fetch(self, source: dict[str, Any], now: datetime) -> FetchedFeed:
        url = source["url"]
        if meetup_find_missing_location(url):
            raise Exception(MEETUP_LOCATION_ERROR)
        html = await fetch_html(url)
        args = parse_meetup_find_page(html)
        if args is None:
            single = parse_meetup_linked_event(html, url)
            if single is None:
                raise Exception("Couldn't read that Meetup link. Use an event page or a search page.")
            errors: list[dict[str, Any]] = []
            events = await with_series_dates([single], now=now, referer=url, errors=errors)
            return FetchedFeed(events=events, errors=errors)

        events = []
        errors = []
        after: str | None = None
        for page in range(MEETUP_SEARCH_MAX_PAGES):
            try:
                result = await fetch_meetup_search_page(args, after, url)
            except Exception as exc:
                if page == 0:
                    raise
                errors.append({"message": f"Meetup search page {page + 1}: {error_message(exc)}"})
                break
            events.extend(result["events"])
            errors.extend(result["errors"])
            if not result["has_next_page"] or not result["next_cursor"] or len(events) >= MEETUP_SEARCH_MAX_EVENTS:
                break
            after = result["next_cursor"]
            await http.sleep(1000)

        cutoff = js.to_ms(now) - DAY_MS
        seen: set[str] = set()
        unique = []
        for event in events:
            if (js.parse_date(event["starts_at"]) or 0) < cutoff or event["source_event_id"] in seen:
                continue
            seen.add(event["source_event_id"])
            unique.append(event)
        unique = unique[:MEETUP_SEARCH_MAX_EVENTS]
        # Meetup ranks search results by relevance and the list reshuffles daily: an event that
        # drops out of it hasn't gone anywhere, so a search never flags events as missing.
        expanded = await with_series_dates(unique, now=now, referer=url, errors=errors)
        return FetchedFeed(events=expanded, errors=errors, coverage="sample")
