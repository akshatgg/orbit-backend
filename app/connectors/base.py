"""Shared shapes for event source connectors.

Every connector returns events as plain dicts so the sync engine never depends on
a platform's own model. The keys are the ones `EVENT_FIELDS` names; all are
optional except `source_event_id`, `title` and `starts_at`.

- source_event_id: stable id within the source (iCal UID, plus the occurrence for recurring events)
- title: plain text, 3–140 characters
- summary: one-line summary, up to 200 characters
- description: plain text, up to 10000 characters
- starts_at / ends_at: ISO-8601 UTC instants (ends_at may be null)
- timezone: IANA zone the source wrote the event in
- format: "in_person" | "online" | "hybrid"
- venue_name / address / city / country: location (country is an ISO 3166-1 alpha-2 code)
- latitude / longitude: floats when the feed gives a GEO point
- url: the event's own public page; cover_url: the platform's image
- price_min / price_max / currency: price range when the platform gives one
- cancelled: the source says this event was cancelled (never auto-cancelled by Orbit)
- tags: list of lowercase tag strings
- series_key / series_label: shared by every date of one recurring series
- interest_labels: the platform's own labels for what the event is about
"""

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

EVENT_FIELDS: tuple[str, ...] = (
    "source_event_id",
    "title",
    "summary",
    "description",
    "starts_at",
    "ends_at",
    "timezone",
    "format",
    "venue_name",
    "address",
    "city",
    "country",
    "latitude",
    "longitude",
    "url",
    "cover_url",
    "price_min",
    "price_max",
    "currency",
    "cancelled",
    "tags",
    "series_key",
    "series_label",
    "interest_labels",
)


@dataclass(slots=True)
class FetchedFeed:
    events: list[dict[str, Any]]
    errors: list[dict[str, Any]] = field(default_factory=list)
    coverage: str | dict[str, Any] = "complete"


class ConnectorError(Exception):
    """A source could not be fetched; the message is shown to the admin and logged on the run."""


@runtime_checkable
class Connector(Protocol):
    async def fetch(self, source: Any) -> FetchedFeed: ...
