"""Shared shapes for event source connectors.

Every connector returns events as plain dicts so the sync engine never depends on a
platform's own model. One event mirrors the TypeScript `NormalizedEvent`, with
snake_case keys:

- source_event_id: stable id within the source (iCal UID, plus the occurrence for recurring events)
- title (3–140), summary (≤200), description (≤5000): already cleaned and clamped
- starts_at / ends_at: ISO-8601 UTC strings as JS `toISOString()` writes them (ends_at may be None)
- all_day: only iCal sets it; not stored
- timezone: IANA zone the source wrote the event in
- format: "in_person" | "online" | "hybrid"
- venue_name / address / city
- country: the platform's own value ("gb", "IN", "United Kingdom"); normalised when synced
- latitude / longitude: floats when the source gives a point
- url: the event's own public page (http/https)
- cancelled: the source says this event was cancelled (never auto-cancelled by Orbit)
- series_key / series_label: shared by every date of one recurring series
- interest_hints: [{"label": str, "weight": int}], what the platform says the event is about
- image_url: the platform's own image (stored as the cover when the event has none)
- price: {"min": float, "max": float, "currency": str} or None; stored on insert only
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

# How much a platform's own label counts toward an interest (see app/sources/interests.py):
# an event's category is strong evidence, a group's category medium, a group's topics weak.
HINT_WEIGHT = {"eventCategory": 3, "groupCategory": 2, "groupTopic": 1}


@dataclass(slots=True)
class FetchedFeed:
    events: list[dict[str, Any]]
    errors: list[dict[str, Any]] = field(default_factory=list)
    # "complete" (default), "sample" (a search: never flag missing) or {"until": iso}.
    coverage: str | dict[str, Any] = "complete"


class ConnectorError(Exception):
    """A source could not be fetched; the message is shown to the admin and logged on the run."""


@runtime_checkable
class Connector(Protocol):
    async def fetch(self, source: dict[str, Any], now: datetime) -> FetchedFeed: ...


def covered_until(events: list[dict[str, Any]]) -> str | dict[str, Any]:
    """A soonest-first list cut off at the cap covers events up to its last start (exclusive)."""
    last = ""
    for event in events:
        if event["starts_at"] > last:
            last = event["starts_at"]
    return {"until": last} if last else "complete"


def error_message(exc: BaseException) -> str:
    """`e instanceof Error ? e.message : String(e)`."""
    return str(exc) if str(exc) else exc.__class__.__name__
