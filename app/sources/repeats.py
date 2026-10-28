"""Group repeating events that the platform didn't mark as a series (port of `repeats.ts`).

Some organisers list each date of a repeating event as its own event (same title, same
place). Those get a shared series key, scoped to the source, so they show as one series.
"""

import re
from typing import Any

from app.connectors import js

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _norm(value: str | None) -> str:
    return js.trim(_NON_ALNUM.sub(" ", (value or "").lower()))


def _key_of(event: dict[str, Any], source_key: str) -> str | None:
    if event.get("format") == "online":
        place = "online"
    else:
        place = _norm(event.get("venue_name")) or _norm(event.get("address")) or _norm(event.get("city"))
    title = _norm(event.get("title"))
    return f"{source_key}:{title}:{place}" if title else None


def group_repeats(events: list[dict[str, Any]], source_key: str) -> list[dict[str, Any]]:
    """Events sharing a title and place (two or more dates) get a common series key."""
    counts: dict[str, int] = {}
    for event in events:
        key = None if event.get("series_key") else _key_of(event, source_key)
        if key:
            counts[key] = counts.get(key, 0) + 1
    out = []
    for event in events:
        if event.get("series_key"):
            out.append(event)
            continue
        key = _key_of(event, source_key)
        out.append({**event, "series_key": key} if key and counts.get(key, 0) > 1 else event)
    return out
