"""iCal (RFC 5545) feeds → normalised events, with recurring events expanded.

Uses `icalendar` for parsing and `dateutil.rrule` for recurrence expansion (no
`recurring_ical_events` dependency). One event id per occurrence: `uid#20261020T180000Z`.
Events that have ended, or start beyond 120 days, are dropped; upstream CANCELLED
status is carried through as `cancelled=True` for the sync engine to flag.
"""

import hashlib
import html
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
from dateutil.rrule import rrulestr
from icalendar import Calendar

from app.core.config import settings

from .base import ConnectorError, FetchedFeed

HORIZON_DAYS = 120
MAX_EVENTS = 2000
MAX_OCCURRENCES = 500
MAX_ITERATIONS = 20000
TITLE_MAX = 140
SUMMARY_MAX = 200
DESCRIPTION_MAX = 10000
TIMEOUT_SECONDS = 15.0
MAX_BYTES = 5 * 1024 * 1024
USER_AGENT = "Orbit event sync (+https://orbit.example)"

ONLINE_HINT = re.compile(r"\b(online|virtual|zoom|teams|google meet|webinar)\b", re.I)
SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
BREAK_RE = re.compile(r"<br\s*/?>", re.I)
BLOCK_END_RE = re.compile(r"</(p|div|li|h[1-6]|tr)>", re.I)
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"[ \t\f\v]+")
BLANK_LINES_RE = re.compile(r"\n{3,}")


def _zone(name: str | None) -> ZoneInfo:
    if name:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return ZoneInfo(settings.DEFAULT_TIMEZONE)


def _valid_zone(name: Any) -> str | None:
    if not isinstance(name, str) or not name:
        return None
    try:
        ZoneInfo(name)
        return name
    except Exception:
        return None


def _as_utc(value: datetime, tz: ZoneInfo) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=tz)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _decoded(component: Any, name: str) -> Any:
    prop = component.get(name)
    if prop is None:
        return None
    value = getattr(prop, "dt", prop)
    return value


def _datetime_prop(component: Any, name: str) -> tuple[datetime, bool] | None:
    value = _decoded(component, name)
    if isinstance(value, datetime):
        return value, False
    if isinstance(value, date):
        return datetime.combine(value, time.min), True
    return None


def _tzid(component: Any, name: str = "DTSTART") -> str | None:
    prop = component.get(name)
    if prop is None:
        return None
    params = getattr(prop, "params", {}) or {}
    raw = params.get("TZID")
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else None
    if not isinstance(raw, str):
        return None
    if _valid_zone(raw):
        return raw
    # Some producers prefix IANA names: "/mozilla.org/20050126_1/Europe/London".
    tail = "/".join(raw.split("/")[-2:])
    return _valid_zone(tail)


def _one_line(value: Any) -> str:
    return SPACE_RE.sub(" ", str(value or "").replace("\r", " ").replace("\n", " ")).strip()


def _clamp(value: str, limit: int) -> str:
    return value[:limit].strip()


def _strip_html(value: str | None) -> str:
    if not value:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = SCRIPT_RE.sub("", text)
    text = COMMENT_RE.sub("", text)
    text = BREAK_RE.sub("\n", text)
    text = BLOCK_END_RE.sub("\n", text)
    text = TAG_RE.sub("", text)
    text = html.unescape(text)
    text = SPACE_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return BLANK_LINES_RE.sub("\n\n", text).strip()


def _http_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if len(value) > 2000 or not re.match(r"^https?://\S+\.\S+", value, re.I):
        return None
    return value


def _geo(component: Any) -> tuple[float | None, float | None]:
    value = _decoded(component, "GEO")
    pair: list[Any] = []
    if isinstance(value, (list, tuple)):
        pair = list(value)
    elif isinstance(value, str):
        pair = re.split(r"[;,]", value)
    elif hasattr(value, "latitude"):
        pair = [getattr(value, "latitude", None), getattr(value, "longitude", None)]
    try:
        latitude = float(pair[0])
        longitude = float(pair[1])
    except (IndexError, TypeError, ValueError):
        return None, None
    if abs(latitude) > 90 or abs(longitude) > 180:
        return None, None
    return latitude, longitude


def _location(value: str | None) -> tuple[str | None, str | None, str]:
    text = _one_line(value)
    if not text:
        return None, None, "in_person"
    if re.match(r"^https?://", text, re.I) or re.match(
        r"^(online|virtual|zoom|microsoft teams|teams|google meet)$", text, re.I
    ):
        return "Online", None, "online"
    comma = text.find(",")
    if comma > 0:
        venue = _clamp(text[:comma], 200) or None
        address = _clamp(text[comma + 1 :], 300) or None
        return venue, address, "in_person"
    online = bool(ONLINE_HINT.search(text)) and comma < 0
    return _clamp(text, 200) or None, None, "online" if online else "in_person"


def _occurrence_key(value: datetime | date) -> str:
    if isinstance(value, datetime):
        utc = value.astimezone(timezone.utc) if value.tzinfo else value
        return utc.strftime("%Y%m%dT%H%M%SZ")
    return value.strftime("%Y%m%d")


def _rule_label(rule: Any) -> str | None:
    freq = rule.get("FREQ") if rule is not None else None
    values = freq if isinstance(freq, (list, tuple)) else [freq]
    key = str(values[0]).upper() if values and values[0] is not None else ""
    return {
        "DAILY": "Repeats daily",
        "WEEKLY": "Repeats weekly",
        "MONTHLY": "Repeats monthly",
        "YEARLY": "Repeats yearly",
    }.get(key)


def _build_event(
    component: Any,
    *,
    uid: str,
    source_event_id: str,
    start: datetime,
    end: datetime | None,
    all_day: bool,
    tz_name: str,
    cancelled: bool,
    series_key: str | None,
    series_label: str | None,
) -> dict[str, Any] | None:
    title = _clamp(_one_line(_decoded(component, "SUMMARY")), TITLE_MAX)
    if len(title) < 3:
        return None
    description = _strip_html(_decoded(component, "DESCRIPTION"))
    summary = _clamp(_one_line(description), SUMMARY_MAX) or None
    venue, address, fmt = _location(_decoded(component, "LOCATION"))
    latitude, longitude = _geo(component)
    url = _http_url(_decoded(component, "URL"))
    return {
        "source_event_id": source_event_id,
        "title": title,
        "summary": summary,
        "description": _clamp(description, DESCRIPTION_MAX) or None,
        "starts_at": _iso(start),
        "ends_at": _iso(end),
        "timezone": tz_name,
        "format": fmt,
        "venue_name": venue,
        "address": address,
        "city": None,
        "country": None,
        "latitude": latitude,
        "longitude": longitude,
        "url": url,
        "cover_url": None,
        "price_min": None,
        "price_max": None,
        "currency": None,
        "cancelled": cancelled,
        "tags": [],
        "series_key": series_key,
        "series_label": series_label,
        "interest_labels": [],
    }


def _exdates(component: Any, tz: ZoneInfo) -> set[str]:
    out: set[str] = set()
    raw = component.get("EXDATE")
    if raw is None:
        return out
    blocks = raw if isinstance(raw, list) else [raw]
    for block in blocks:
        for item in getattr(block, "dts", []) or []:
            value = getattr(item, "dt", None)
            if isinstance(value, datetime):
                out.add(_occurrence_key(_as_utc(value, tz)))
            elif isinstance(value, date):
                out.add(_occurrence_key(value))
    return out


def _rdates(component: Any, tz: ZoneInfo) -> list[datetime]:
    out: list[datetime] = []
    raw = component.get("RDATE")
    if raw is None:
        return out
    blocks = raw if isinstance(raw, list) else [raw]
    for block in blocks:
        for item in getattr(block, "dts", []) or []:
            value = getattr(item, "dt", None)
            if isinstance(value, datetime):
                out.append(_as_utc(value, tz))
            elif isinstance(value, date):
                out.append(_as_utc(datetime.combine(value, time.min), tz))
    return out


def _calendar_zone(calendar: Any) -> str:
    wr_tz = _decoded(calendar, "X-WR-TIMEZONE")
    return _valid_zone(wr_tz) or _valid_zone(settings.DEFAULT_TIMEZONE) or "Europe/London"


def parse_ical(text: str, now: datetime) -> FetchedFeed:
    """Parse iCal text into events, expanding recurrences over the next 120 days."""
    if "BEGIN:VCALENDAR" not in text.upper():
        raise ConnectorError("This doesn't look like an iCal feed (no BEGIN:VCALENDAR).")
    try:
        calendar = Calendar.from_ical(text)
    except Exception as exc:  # noqa: BLE001 - surface the parser's reason to the admin
        raise ConnectorError(f"Couldn't read the iCal feed: {exc}") from exc

    now = now.astimezone(timezone.utc)
    horizon = now + timedelta(days=HORIZON_DAYS)
    calendar_tz = _calendar_zone(calendar)

    masters: dict[str, Any] = {}
    exceptions: dict[str, dict[str, Any]] = {}
    for component in calendar.walk("VEVENT"):
        uid = _one_line(_decoded(component, "UID"))[:400]
        if not uid:
            start = _datetime_prop(component, "DTSTART")
            seed = f"{_one_line(_decoded(component, 'SUMMARY'))}|{start[0] if start else ''}"
            uid = "nouid-" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]
        recurrence_id = _datetime_prop(component, "RECURRENCE-ID")
        if recurrence_id:
            zone = _zone(_tzid(component) or calendar_tz)
            key = _occurrence_key(_as_utc(recurrence_id[0], zone))
            exceptions.setdefault(uid, {})[key] = component
        elif uid not in masters:
            masters[uid] = component

    events: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    seen: set[str] = set()

    def push(event: dict[str, Any] | None, ref: str) -> None:
        if event is None:
            errors.append({"event": ref, "message": "Skipped: title is missing or too short"})
            return
        if len(events) >= MAX_EVENTS:
            return
        if event["source_event_id"] in seen:
            return
        seen.add(event["source_event_id"])
        events.append(event)

    for uid, component in masters.items():
        if len(events) >= MAX_EVENTS:
            break
        try:
            start_prop = _datetime_prop(component, "DTSTART")
            if start_prop is None:
                errors.append({"event": uid, "message": "Skipped: no start date"})
                continue
            tz_name = _tzid(component) or calendar_tz
            tz = _zone(tz_name)
            start_raw, all_day = start_prop
            start_utc = _as_utc(start_raw, tz)

            end_raw: datetime | None = None
            end_prop = _datetime_prop(component, "DTEND")
            if end_prop:
                end_raw = end_prop[0]
            else:
                duration = _decoded(component, "DURATION")
                if isinstance(duration, timedelta):
                    end_raw = start_raw + duration
            end_utc = _as_utc(end_raw, tz) if end_raw else None
            if all_day and end_utc:
                end_utc = end_utc - timedelta(minutes=1)
            if end_utc and end_utc <= start_utc:
                end_utc = start_utc + timedelta(days=1) - timedelta(minutes=1) if all_day else None

            duration = (end_utc - start_utc) if end_utc else timedelta(0)
            rule_prop = component.get("RRULE")
            status = _one_line(_decoded(component, "STATUS")).upper()
            cancelled = status == "CANCELLED"

            if rule_prop is None:
                if max(start_utc, end_utc or start_utc) < now or start_utc > horizon:
                    continue
                push(
                    _build_event(
                        component,
                        uid=uid,
                        source_event_id=uid,
                        start=start_utc,
                        end=end_utc,
                        all_day=all_day,
                        tz_name=tz_name,
                        cancelled=cancelled,
                        series_key=None,
                        series_label=None,
                    ),
                    uid,
                )
                continue

            series_label = _rule_label(rule_prop)
            rule_dtstart = start_raw if start_raw.tzinfo else start_raw.replace(tzinfo=tz)
            try:
                rule = rrulestr(rule_prop.to_ical().decode(), dtstart=rule_dtstart)
            except Exception as exc:  # noqa: BLE001 - one bad rule shouldn't fail the feed
                errors.append({"event": uid, "message": f"Skipped recurring event: {exc}"})
                continue

            exdates = _exdates(component, tz)
            extra_rdates = _rdates(component, tz)
            window_start = now - duration
            occurrences: list[datetime] = []
            try:
                for index, occurrence in enumerate(rule.xafter(window_start, inc=True)):
                    if index >= MAX_ITERATIONS or occurrence > horizon:
                        break
                    occurrences.append(occurrence.astimezone(timezone.utc) if occurrence.tzinfo else _as_utc(occurrence, tz))
            except Exception as exc:  # noqa: BLE001
                errors.append({"event": uid, "message": f"Skipped recurring event: {exc}"})
                continue
            occurrences.extend(extra_rdates)
            occurrences.sort()

            kept = 0
            used_exceptions: set[str] = set()
            for occurrence_start in occurrences:
                if kept >= MAX_OCCURRENCES or len(events) >= MAX_EVENTS:
                    break
                key = _occurrence_key(occurrence_start)
                if key in exdates:
                    continue
                occurrence_end = occurrence_start + duration
                if max(occurrence_start, occurrence_end) < now or occurrence_start > horizon:
                    continue
                occurrence_id = f"{uid}#{key}"
                override = exceptions.get(uid, {}).get(key)
                if override is not None:
                    used_exceptions.add(key)
                    over_start_prop = _datetime_prop(override, "DTSTART")
                    over_end_prop = _datetime_prop(override, "DTEND")
                    over_tz_name = _tzid(override) or tz_name
                    over_tz = _zone(over_tz_name)
                    over_start = (
                        _as_utc(over_start_prop[0], over_tz) if over_start_prop else occurrence_start
                    )
                    over_end = _as_utc(over_end_prop[0], over_tz) if over_end_prop else over_start + duration
                    over_status = _one_line(_decoded(override, "STATUS")).upper()
                    push(
                        _build_event(
                            override,
                            uid=uid,
                            source_event_id=occurrence_id,
                            start=over_start,
                            end=over_end,
                            all_day=all_day,
                            tz_name=over_tz_name,
                            cancelled=over_status == "CANCELLED",
                            series_key=uid,
                            series_label=series_label,
                        ),
                        occurrence_id,
                    )
                else:
                    push(
                        _build_event(
                            component,
                            uid=uid,
                            source_event_id=occurrence_id,
                            start=occurrence_start,
                            end=occurrence_end,
                            all_day=all_day,
                            tz_name=tz_name,
                            cancelled=cancelled,
                            series_key=uid,
                            series_label=series_label,
                        ),
                        occurrence_id,
                    )
                kept += 1

            for key, override in exceptions.get(uid, {}).items():
                if key in used_exceptions or len(events) >= MAX_EVENTS:
                    continue
                over_start_prop = _datetime_prop(override, "DTSTART")
                if over_start_prop is None:
                    continue
                over_tz_name = _tzid(override) or tz_name
                over_tz = _zone(over_tz_name)
                over_start = _as_utc(over_start_prop[0], over_tz)
                over_end_prop = _datetime_prop(override, "DTEND")
                over_end = _as_utc(over_end_prop[0], over_tz) if over_end_prop else None
                if max(over_start, over_end or over_start) < now or over_start > horizon:
                    continue
                over_status = _one_line(_decoded(override, "STATUS")).upper()
                push(
                    _build_event(
                        override,
                        uid=uid,
                        source_event_id=f"{uid}#{key}",
                        start=over_start,
                        end=over_end,
                        all_day=all_day,
                        tz_name=over_tz_name,
                        cancelled=over_status == "CANCELLED",
                        series_key=uid,
                        series_label=series_label,
                    ),
                    f"{uid}#{key}",
                )
        except Exception as exc:  # noqa: BLE001 - keep the rest of the feed
            errors.append({"event": uid, "message": f"Skipped: {exc}"})

    for uid, overrides in exceptions.items():
        if uid in masters:
            continue
        for key, component in overrides.items():
            if len(events) >= MAX_EVENTS:
                break
            start_prop = _datetime_prop(component, "DTSTART")
            if start_prop is None:
                continue
            tz = _zone(_tzid(component) or calendar_tz)
            start = _as_utc(start_prop[0], tz)
            end_prop = _datetime_prop(component, "DTEND")
            end = _as_utc(end_prop[0], tz) if end_prop else None
            if max(start, end or start) < now or start > horizon:
                continue
            status = _one_line(_decoded(component, "STATUS")).upper()
            push(
                _build_event(
                    component,
                    uid=uid,
                    source_event_id=f"{uid}#{key}",
                    start=start,
                    end=end,
                    all_day=start_prop[1],
                    tz_name=_tzid(component) or calendar_tz,
                    cancelled=status == "CANCELLED",
                    series_key=uid,
                    series_label=None,
                ),
                f"{uid}#{key}",
            )

    events.sort(key=lambda event: event["starts_at"] or "")
    return FetchedFeed(events=events, errors=errors, coverage="complete")


class IcalConnector:
    """Fetches an iCal URL and expands it into normalised events."""

    async def fetch(self, source: Any) -> FetchedFeed:
        url = str(getattr(source, "url", None) or (source.get("url") if isinstance(source, dict) else "") or "")
        if not url.lower().startswith(("http://", "https://")):
            raise ConnectorError("The source URL must start with http:// or https://.")
        host = urlparse(url).hostname or ""
        if not host:
            raise ConnectorError("The source URL has no host.")
        try:
            async with httpx.AsyncClient(
                timeout=TIMEOUT_SECONDS,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT, "Accept": "text/calendar, text/plain;q=0.8, */*;q=0.1"},
            ) as client:
                response = await client.get(url)
        except httpx.HTTPError as exc:
            raise ConnectorError(f"Couldn't reach the feed ({exc.__class__.__name__}).") from exc
        if response.status_code in (429, 503):
            raise ConnectorError(
                f"The feed asked us to slow down (HTTP {response.status_code}). We'll try again on the next refresh."
            )
        if response.status_code >= 400:
            raise ConnectorError(f"The feed returned HTTP {response.status_code}.")
        body = response.content
        if len(body) > MAX_BYTES:
            raise ConnectorError("The feed is larger than 5 MB.")
        text = body.decode(response.encoding or "utf-8", errors="replace")
        now = datetime.now(timezone.utc)
        return parse_ical(text, now)
