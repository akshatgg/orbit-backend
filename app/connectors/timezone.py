"""Wall-clock time in an IANA zone → UTC instant (port of `src/lib/sources/timezone.ts`).

`zoned_time_to_utc` follows the TypeScript algorithm literally rather than attaching a
`ZoneInfo`: a time skipped by a DST jump moves forward, and an ambiguous time resolves to
the later (standard-time) instant, where `datetime(..., tzinfo=ZoneInfo)` would pick the
earlier one.
"""

import calendar
from datetime import datetime, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, available_timezones

DEFAULT_TIMEZONE = "Europe/London"


@lru_cache(maxsize=1)
def _zones() -> dict[str, str]:
    names = {name.lower(): name for name in available_timezones()}
    for extra in ("UTC", "GMT", "Etc/UTC", "Etc/GMT"):
        names.setdefault(extra.lower(), extra)
    return names


def canonical(tz: str | None) -> str | None:
    """The zone name zoneinfo knows, matched case-insensitively as Intl does; None if unknown."""
    if not isinstance(tz, str) or not tz:
        return None
    return _zones().get(tz.lower())


def is_valid_timezone(tz: object) -> bool:
    return isinstance(tz, str) and canonical(tz) is not None


@lru_cache(maxsize=512)
def _zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def _offset_at(ms: int, tz: str) -> int:
    """Offset of `tz` from UTC at the instant `ms`, in milliseconds (whole seconds, as Intl)."""
    seconds = ms // 1000
    local = datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone(_zone(canonical(tz) or DEFAULT_TIMEZONE))
    as_utc = calendar.timegm((local.year, local.month, local.day, local.hour, local.minute, local.second)) * 1000
    return as_utc - seconds * 1000


def zoned_time_to_utc(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0, *, tz: str
) -> int | None:
    """The UTC instant (ms) at which clocks in `tz` show the given wall time; None if invalid."""
    try:
        wall = calendar.timegm((year, month, day, hour, minute, second)) * 1000
    except (OverflowError, ValueError):
        return None
    first = wall - _offset_at(wall, tz)
    first_offset = _offset_at(first, tz)
    if wall - first_offset == first:
        return first
    second_try = wall - first_offset
    if wall - _offset_at(second_try, tz) == second_try:
        return second_try
    return max(first, second_try)  # inside a DST gap


def local_day(ms: int, tz: str | None) -> str:
    """The calendar day ("2026-10-22") an instant falls on in `tz` (London when invalid)."""
    zone = canonical(tz) or DEFAULT_TIMEZONE
    local = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(_zone(zone))
    return local.strftime("%Y-%m-%d")


def valid_or_london(tz: object) -> str:
    """The zone as given when Intl would accept it, else Europe/London."""
    return tz if isinstance(tz, str) and is_valid_timezone(tz) else DEFAULT_TIMEZONE

