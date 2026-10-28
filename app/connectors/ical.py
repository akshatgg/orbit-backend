"""iCal (RFC 5545) → normalised events (port of `src/lib/sources/ical.ts`).

The TypeScript engine parses feeds with ical.js; this follows its rules with `icalendar`
for the parsing and `dateutil` for RRULE expansion:

- A time is *resolved* (an absolute instant) when it is UTC (`Z`, or TZID UTC/GMT/Z) or
  its TZID has a VTIMEZONE in the feed. Any other time is floating and is read in the
  event's zone (its TZID when that is an IANA name, else the calendar's) with the literal
  `zoned_time_to_utc`; all-day dates likewise.
- One-off events are kept until they end and up to `horizon_days` ahead; recurring
  events are expanded `recurrence_days` ahead, at most `max_occurrences` dates each.
- Occurrence ids are `uid#<key>`: `YYYYMMDDTHHMMSSZ` for resolved times, the local
  `YYYYMMDDTHHMMSS` for floating ones, `YYYYMMDD` for all-day dates. A feed without UIDs
  gets `nouid-<FNV-1a base36 of summary|dtstart>`.
"""

import calendar as calendar_lib
import re
import warnings
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr
from icalendar import Calendar

from . import fetch as http
from . import js, platforms
from .base import ConnectorError, FetchedFeed
from .text import LIMITS, clamp, clean_prose, one_line, or_null, safe_http_url, strip_html, summarise
from .timezone import DEFAULT_TIMEZONE, canonical, is_valid_timezone, zoned_time_to_utc

ICAL_DEFAULTS = {"recurrence_days": 60, "max_occurrences": 30, "horizon_days": 365, "max_events": 2000}

# Safety net for rules like FREQ=MINUTELY that started years ago.
MAX_ITERATIONS = 20000
DAY = 24 * 60 * 60 * 1000

_UTC_IDS = {"Z", "UTC", "GMT"}
_VALUE = re.compile(r"^(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2})(Z)?)?$")


@dataclass(frozen=True)
class ITime:
    """An ical.js `Time`: wall-clock fields plus how its zone was resolved."""

    wall: datetime  # naive
    is_date: bool
    kind: str  # "utc" | "zone" (a VTIMEZONE in the feed) | "floating"
    tz: tzinfo | None = None

    @property
    def resolved(self) -> bool:
        return self.kind in ("utc", "zone")

    def unix_ms(self) -> int:
        """`toUnixTime()`: floating times count as UTC."""
        if self.kind == "zone" and self.tz is not None:
            return js.to_ms(_localize(self.wall, self.tz))
        return calendar_lib.timegm(self.wall.timetuple()) * 1000

    def to_string(self) -> str:
        w = self.wall
        if self.is_date:
            return f"{w.year}-{w.month:02d}-{w.day:02d}"
        out = f"{w.year}-{w.month:02d}-{w.day:02d}T{w.hour:02d}:{w.minute:02d}:{w.second:02d}"
        return out + ("Z" if self.kind == "utc" else "")

    def ical_string(self) -> str:
        return self.to_string().replace("-", "").replace(":", "")

    def to_utc(self) -> "ITime":
        """`convertToZone(utcTimezone)`: a floating time keeps its wall clock."""
        if self.kind == "zone" and self.tz is not None:
            moment = js.from_ms(self.unix_ms()).replace(tzinfo=None)
            return ITime(moment, self.is_date, "utc")
        return ITime(self.wall, self.is_date, "utc")

    def plus(self, delta: timedelta) -> "ITime":
        return replace(self, wall=self.wall + delta)


def _localize(wall: datetime, tz: tzinfo) -> datetime:
    localize = getattr(tz, "localize", None)  # pytz-style zones
    return localize(wall) if localize else wall.replace(tzinfo=tz)


def _first(component: Any, name: str) -> Any:
    value = component.get(name)
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _text_prop(component: Any, name: str) -> str | None:
    value = _first(component, name)
    if value is None:
        return None
    return str(value) if isinstance(value, str) else _as_text(value)


def _as_text(value: Any) -> str | None:
    try:
        raw = value.to_ical()
    except Exception:  # noqa: BLE001
        return None
    return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)


def _param(prop: Any, name: str) -> Any:
    params = getattr(prop, "params", None) or {}
    value = params.get(name)
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


class _Zones:
    """The calendar's VTIMEZONEs by TZID (ical.js getTimeZoneByID)."""

    def __init__(self, cal: Any) -> None:
        self.components = {}
        for sub in cal.subcomponents:
            if sub.name == "VTIMEZONE":
                tzid = _text_prop(sub, "TZID")
                if tzid is not None and tzid not in self.components:
                    self.components[tzid] = sub
        self._cache: dict[str, tzinfo | None] = {}

    def get(self, tzid: str) -> tzinfo | None:
        if tzid not in self.components:
            return None
        if tzid not in self._cache:
            name = canonical(tzid)
            zone: tzinfo | None = None
            if name:
                zone = ZoneInfo(name)
            else:
                try:
                    zone = self.components[tzid].to_tz()
                except Exception:  # noqa: BLE001 - an unusable definition reads as UTC
                    zone = timezone.utc
            self._cache[tzid] = zone
        return self._cache[tzid]


def _parse_value(raw: str, tzid: Any, zones: _Zones) -> ITime | None:
    match = _VALUE.match(raw.strip())
    if not match:
        return None
    y, mo, d, h, mi, s, z = match.groups()
    try:
        if h is None:
            return ITime(datetime(int(y), int(mo), int(d)), True, "floating")
        wall = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s))
    except ValueError:
        return None
    if z:
        return ITime(wall, False, "utc")
    if isinstance(tzid, str) and tzid:
        zone = zones.get(tzid)
        if zone is not None:
            return ITime(wall, False, "zone", zone)
        if tzid in _UTC_IDS:
            return ITime(wall, False, "utc")
    return ITime(wall, False, "floating")


def _time_prop(component: Any, name: str, zones: _Zones) -> ITime | None:
    prop = _first(component, name)
    if prop is None:
        return None
    raw = _as_text(prop)
    return _parse_value(raw, _param(prop, "TZID"), zones) if raw is not None else None


def _time_list(component: Any, name: str, zones: _Zones) -> list[ITime]:
    value = component.get(name)
    if value is None:
        return []
    out: list[ITime] = []
    for block in value if isinstance(value, list) else [value]:
        tzid = _param(block, "TZID")
        for item in getattr(block, "dts", []) or []:
            raw = _as_text(item)
            if raw is None:
                continue
            parsed = _parse_value(raw.split("/")[0], tzid, zones)  # a PERIOD counts from its start
            if parsed is not None:
                out.append(parsed)
    return out


def _duration_prop(component: Any) -> timedelta | None:
    prop = _first(component, "DURATION")
    value = getattr(prop, "dt", None) if prop is not None else None
    return value if isinstance(value, timedelta) else None


def _end_date(component: Any, start: ITime, zones: _Zones) -> ITime:
    """ical.js `Event.endDate`: DTEND, else DTSTART + DURATION, else DTSTART (+1 day if all-day)."""
    end = _time_prop(component, "DTEND", zones)
    if end is not None:
        return end
    duration = _duration_prop(component)
    if duration is not None:
        return start.plus(duration)
    return start.plus(timedelta(days=1)) if start.is_date else start


def _to_instant(t: ITime, tz: str) -> int | None:
    w = t.wall
    if t.is_date:
        return zoned_time_to_utc(w.year, w.month, w.day, tz=tz)
    if t.resolved:
        return t.unix_ms()
    return zoned_time_to_utc(w.year, w.month, w.day, w.hour, w.minute, w.second, tz=tz)


def _event_timezone(component: Any, calendar_tz: str) -> str:
    """The zone this event was written in: its TZID if we know it, else the calendar's."""
    prop = _first(component, "DTSTART")
    raw = _param(prop, "TZID") if prop is not None else None
    if isinstance(raw, str):
        if is_valid_timezone(raw):
            return raw
        # Some producers prefix IANA names, e.g. "/mozilla.org/20050126_1/Europe/London".
        tail = "/".join(raw.split("/")[-2:])
        if is_valid_timezone(tail):
            return tail
    return calendar_tz


_ONLINE_HINT = re.compile(r"\b(online|virtual|zoom|teams|google meet|webinar)\b", re.IGNORECASE | re.ASCII)
_ONLINE_NAME = re.compile(r"^(online|virtual|zoom|microsoft teams|teams|google meet)$", re.IGNORECASE | re.ASCII)
_LINK = re.compile(r"^https?://", re.IGNORECASE | re.ASCII)


def _split_location(location: str | None) -> tuple[str | None, str | None, str]:
    text = one_line(location)
    if not text:
        return None, None, "in_person"
    # A bare link (often a meeting URL): never publish it; just mark the event as online.
    if _LINK.match(text) or _ONLINE_NAME.match(text):
        return "Online", None, "online"
    comma = text.find(",")
    venue = js.trim(text[:comma]) if comma > 0 else text
    address = js.trim(text[comma + 1 :]) if comma > 0 else ""
    online = bool(_ONLINE_HINT.search(text)) and comma < 0
    return (
        or_null(clamp(venue, LIMITS["venue"])),
        or_null(clamp(address, LIMITS["address"])),
        "online" if online else "in_person",
    )


def stable_hash(value: str) -> str:
    """FNV-1a over UTF-16 code units, in base 36 (used only when a feed omits UIDs)."""
    h = 0x811C9DC5
    data = value.encode("utf-16-le", "surrogatepass")
    for i in range(0, len(data), 2):
        h ^= data[i] | (data[i + 1] << 8)
        h = (h * 0x01000193) & 0xFFFFFFFF
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        h, rem = divmod(h, 36)
        out = digits[rem] + out
        if h == 0:
            return out


def _geo_point(component: Any) -> tuple[float | None, float | None]:
    """GEO is a structured `lat;lon` value. Missing or nonsensical points are dropped."""
    prop = _first(component, "GEO")
    if prop is None:
        return None, None
    if hasattr(prop, "latitude") and hasattr(prop, "longitude"):
        pair: list[Any] = [prop.latitude, prop.longitude]
    else:
        pair = re.split(r"[;,]", _as_text(prop) or "")
    if len(pair) < 2:
        return None, None
    latitude, longitude = js.number(pair[0]), js.number(pair[1])
    if not js.finite(latitude) or not js.finite(longitude) or abs(latitude) > 90 or abs(longitude) > 180:
        return None, None
    return latitude, longitude


def _base_uid(component: Any) -> str:
    uid = _text_prop(component, "UID")
    uid = js.trim(uid) if uid is not None else ""
    if uid:
        return js.u16slice(uid, 400)
    start = _first(component, "DTSTART")
    start_text = ""
    if start is not None:
        raw = _as_text(start)
        parsed = _parse_value(raw, _param(start, "TZID"), _NO_ZONES) if raw else None
        start_text = parsed.to_string() if parsed else ""
    summary = _text_prop(component, "SUMMARY") or ""
    return f"nouid-{stable_hash(f'{summary}|{start_text}')}"


def _occurrence_key(t: ITime) -> str:
    """iCal-style key for an occurrence, e.g. "20261020T180000Z" or "20261020" for all-day."""
    return (t.to_utc() if t.resolved and not t.is_date else t).ical_string()


def _normalise(
    component: Any, start: ITime, end: ITime | None, source_event_id: str, calendar_tz: str
) -> dict[str, Any]:
    title = clamp(one_line(_text_prop(component, "SUMMARY")), LIMITS["title"])
    if js.u16len(title) < 3:
        return {"event": source_event_id, "message": "Skipped: title is missing or too short"}

    tz = _event_timezone(component, calendar_tz)
    all_day = start.is_date
    starts = _to_instant(start, tz)
    ends = _to_instant(end, tz) if end is not None else None
    if all_day and ends is not None:
        ends -= 60 * 1000  # DTEND is exclusive → 23:59 of the last day
    if ends is not None and (starts is None or ends <= starts):
        ends = starts + DAY - 60000 if all_day and starts is not None else None
    if starts is None:
        return {"event": source_event_id, "message": "Skipped: invalid start date"}

    description = or_null(
        clamp(clean_prose(strip_html(_text_prop(component, "DESCRIPTION")), keep_emphasis=True), LIMITS["description"])
    )
    venue_name, address, fmt = _split_location(_text_prop(component, "LOCATION"))
    latitude, longitude = _geo_point(component)
    status = _text_prop(component, "STATUS")

    return {
        "source_event_id": source_event_id,
        "title": title,
        "summary": summarise(description),
        "description": description,
        "starts_at": js.iso(starts),
        "ends_at": js.iso(ends) if ends is not None else None,
        "all_day": all_day,
        "timezone": tz,
        "format": fmt,
        "venue_name": venue_name,
        "address": address,
        "city": None,
        "latitude": latitude,
        "longitude": longitude,
        "url": safe_http_url(_text_prop(component, "URL")),
        "cancelled": (status or "").upper() == "CANCELLED",
    }


class _NoZones(_Zones):
    def __init__(self) -> None:
        self.components = {}
        self._cache = {}


_NO_ZONES = _NoZones()

_UNTIL = re.compile(r"(^|;)UNTIL=([^;]*)", re.IGNORECASE)


def _rule_times(rule_text: str, dtstart: ITime, zones: _Zones) -> Iterator[ITime]:
    """One RRULE's occurrences from DTSTART, in DTSTART's zone; UNTIL compared as ical.js does."""
    until: ITime | None = None
    match = _UNTIL.search(rule_text)
    if match:
        until = _parse_value(match.group(2), None, zones)
        rule_text = _UNTIL.sub("", rule_text).lstrip(";")
    rule = rrulestr(rule_text, dtstart=dtstart.wall)
    for wall in rule:
        occurrence = ITime(wall, dtstart.is_date, dtstart.kind, dtstart.tz)
        if until is not None and occurrence.unix_ms() > until.unix_ms():
            return
        yield occurrence


def _expansion(component: Any, dtstart: ITime, zones: _Zones) -> Iterator[ITime]:
    """ical.js RecurExpansion: RRULE dates and RDATEs merged in order, EXDATEs removed."""
    iterators = []
    rules = component.get("RRULE")
    for prop in rules if isinstance(rules, list) else [rules]:
        if prop is not None:
            iterators.append(_rule_times(_as_text(prop) or "", dtstart, zones))
    heads: list[ITime | None] = [next(it, None) for it in iterators]

    # Every RDATE counts (ical.js keeps them all once the first precedes DTSTART, and the
    # rest are after it anyway).
    rdates = sorted(_time_list(component, "RDATE", zones), key=lambda t: t.unix_ms())
    exdates = _time_list(component, "EXDATE", zones)
    ex_times = {t.unix_ms() for t in exdates if not t.is_date}
    ex_dates = {t.unix_ms() for t in exdates if t.is_date}
    ex_days = {t.wall.date() for t in exdates if t.is_date}

    def excluded(t: ITime) -> bool:
        # A DATE EXDATE removes every time on that day (ical.js _compare_special).
        if t.is_date:
            return t.unix_ms() in ex_times or t.unix_ms() in ex_dates
        return t.wall.date() in ex_days or t.unix_ms() in ex_times

    r = 0
    while True:
        live = [(h.unix_ms(), i) for i, h in enumerate(heads) if h is not None]
        rdate = rdates[r] if r < len(rdates) else None
        if rdate is None and not live:
            return
        if live:
            best_ms, best = min(live)
        if rdate is not None and (not live or rdate.unix_ms() <= best_ms):
            occurrence = rdate
            r += 1
        else:
            occurrence = heads[best]  # type: ignore[assignment]
            heads[best] = next(iterators[best], None)
        if excluded(occurrence):
            continue
        yield occurrence


def parse_ical(
    text: str,
    now: datetime,
    *,
    default_timezone: str | None = None,
    recurrence_days: int = 60,
    max_occurrences: int = 30,
    horizon_days: int = 365,
    max_events: int = 2000,
) -> FetchedFeed:
    """Parses an iCal feed. Throws when the text isn't iCal at all."""
    now_ms = js.to_ms(now)
    single_horizon = now_ms + horizon_days * DAY
    recurrence_end = now_ms + recurrence_days * DAY

    if not re.search("BEGIN:VCALENDAR", text, re.IGNORECASE):
        raise ConnectorError("This doesn't look like an iCal feed (no BEGIN:VCALENDAR).")
    try:
        with warnings.catch_warnings():
            # icalendar warns when it guesses a vendor-prefixed TZID; the guess is what we want.
            warnings.simplefilter("ignore")
            cal = Calendar.from_ical(text)
    except Exception as exc:  # noqa: BLE001 - surface the parser's reason to the admin
        raise ConnectorError(f"Couldn't read the iCal feed: {exc}") from exc

    zones = _Zones(cal)
    wr_tz = _text_prop(cal, "X-WR-TIMEZONE")
    if is_valid_timezone(wr_tz):
        calendar_tz = wr_tz
    elif is_valid_timezone(default_timezone):
        calendar_tz = default_timezone
    else:
        calendar_tz = DEFAULT_TIMEZONE

    events: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    seen: set[str] = set()

    def push(result: dict[str, Any]) -> None:
        if "message" in result:
            errors.append(result)
            return
        if result["source_event_id"] in seen:
            return
        seen.add(result["source_event_id"])
        events.append(result)

    # Group VEVENTs: masters by UID, and their modified occurrences (RECURRENCE-ID).
    masters: dict[str, Any] = {}
    exceptions: dict[str, list[Any]] = {}
    for vevent in (c for c in cal.subcomponents if c.name == "VEVENT"):
        uid = _base_uid(vevent)
        if vevent.get("RECURRENCE-ID") is not None:
            exceptions.setdefault(uid, []).append(vevent)
        elif uid not in masters:
            masters[uid] = vevent

    def keep_one_off(start_ms: int | None, end_ms: int | None) -> bool:
        if start_ms is None or end_ms is None:
            return True  # NaN comparisons are false in JS
        return not (max(start_ms, end_ms) < now_ms or start_ms > single_horizon)

    for uid, component in masters.items():
        if len(events) >= max_events:
            break
        try:
            related: dict[str, Any] = {}
            for exception in exceptions.pop(uid, []):
                recurrence_id = _time_prop(exception, "RECURRENCE-ID", zones)
                if recurrence_id is None:
                    raise ValueError("invalid recurrence-id")
                related[recurrence_id.to_string()] = exception
            start = _time_prop(component, "DTSTART", zones)
            if start is None:
                errors.append({"event": uid, "message": "Skipped: no start date"})
                continue
            tz = _event_timezone(component, calendar_tz)
            end = _end_date(component, start, zones)

            if component.get("RRULE") is None and component.get("RDATE") is None:
                if not keep_one_off(_to_instant(start, tz), _to_instant(end, tz)):
                    continue
                push(_normalise(component, start, end, uid, calendar_tz))
                continue

            duration = _duration_prop(component)
            if duration is None:
                duration = timedelta(milliseconds=end.unix_ms() - start.unix_ms())
            kept = 0
            for i, occurrence in enumerate(_expansion(component, start, zones)):
                if i >= MAX_ITERATIONS:
                    break
                occurrence_start = _to_instant(occurrence, tz)
                if occurrence_start is not None and occurrence_start > recurrence_end:
                    break
                exception = related.get(occurrence.to_string()) or related.get(occurrence.to_utc().to_string())
                if exception is not None:
                    item = exception
                    item_start = _time_prop(exception, "DTSTART", zones)
                    if item_start is None:
                        raise ValueError("exception without a start")
                    item_end = _end_date(exception, item_start, zones)
                else:
                    item, item_start, item_end = component, occurrence, occurrence.plus(duration)
                end_ms = _to_instant(item_end, tz)
                if occurrence_start is not None and end_ms is not None and max(occurrence_start, end_ms) < now_ms:
                    continue
                push(_normalise(item, item_start, item_end, f"{uid}#{_occurrence_key(occurrence)}", calendar_tz))
                kept += 1
                if kept >= max_occurrences or len(events) >= max_events:
                    break
        except Exception as exc:  # noqa: BLE001 - keep the rest of the feed
            errors.append({"event": uid, "message": f"Skipped: {exc}"})

    # Modified occurrences whose series isn't in the feed: treat each as a one-off.
    for uid, items in exceptions.items():
        for component in items:
            if len(events) >= max_events:
                break
            try:
                start = _time_prop(component, "DTSTART", zones)
                if start is None:
                    raise ValueError("no start date")
                tz = _event_timezone(component, calendar_tz)
                end = _end_date(component, start, zones)
                if not keep_one_off(_to_instant(start, tz), _to_instant(end, tz)):
                    continue
                recurrence_id = _time_prop(component, "RECURRENCE-ID", zones)
                if recurrence_id is None:
                    raise ValueError("invalid recurrence-id")
                push(_normalise(component, start, end, f"{uid}#{_occurrence_key(recurrence_id)}", calendar_tz))
            except Exception as exc:  # noqa: BLE001
                errors.append({"event": uid, "message": f"Skipped: {exc}"})

    events.sort(key=lambda event: event["starts_at"])
    return FetchedFeed(events=events, errors=errors)


class IcalConnector:
    """iCal feeds, with the platform's own clean-up for Luma / Meetup (`feedConnector`)."""

    async def fetch(self, source: dict[str, Any], now: datetime) -> FetchedFeed:
        text = await http.fetch_feed_text(source["url"])
        result = parse_ical(text, now)
        kind = source.get("kind") or "ical"
        result.events = [platforms.clean_event(kind, event) for event in result.events]
        return result
