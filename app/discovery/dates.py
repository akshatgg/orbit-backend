"""UK-local date presets for the Explore date filter (port of `datePresetRange` in the frontend)."""

from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

UK = ZoneInfo("Europe/London")

DatePreset = Literal["today", "week", "weekend", "month"]


def uk_start_of_day(day: date) -> datetime:
    """The instant UK midnight starts on `day`."""
    return datetime(day.year, day.month, day.day, tzinfo=UK)


def date_preset_range(preset: DatePreset, now: datetime) -> tuple[datetime, datetime]:
    """[from, to) for a date filter, starting no earlier than `now`."""
    today = now.astimezone(UK).date()
    if preset == "today":
        return now, uk_start_of_day(today + timedelta(days=1))
    if preset == "week":
        return now, now + timedelta(days=7)
    if preset == "month":
        return now, now + timedelta(days=30)
    # This coming Saturday, or the current one when it's already the weekend.
    saturday = today + timedelta(days=5 - today.weekday())
    start = uk_start_of_day(saturday)
    return (start if start > now else now), uk_start_of_day(saturday + timedelta(days=2))
