"""Connector registry: one entry per `event_sources.kind`."""

from typing import Any

from .base import ConnectorError, FetchedFeed
from .ical import IcalConnector


_MODULES = {
    "Luma category/discover": "luma-discover.ts",
    "Meetup search": "meetup-search.ts",
    "Eventbrite": "eventbrite.ts",
    "Fatsoma": "fatsoma.ts",
}


class UnsupportedConnector:
    """A platform connector that this backend does not port yet."""

    def __init__(self, label: str, module: str | None = None) -> None:
        self.label = label
        self.module = module or _MODULES.get(label, label.lower().replace(" ", "-") + ".ts")

    async def fetch(self, source: Any) -> FetchedFeed:
        raise ConnectorError(
            f"{self.label} sources aren't supported by this backend yet "
            f"(replaces the TypeScript module src/lib/sources/{self.module})."
        )


CONNECTORS: dict[str, Any] = {
    "ical": IcalConnector(),
    "luma": IcalConnector(),
    "meetup": IcalConnector(),
    "luma-discover": UnsupportedConnector("Luma category/discover", "luma-discover.ts"),
    "meetup-search": UnsupportedConnector("Meetup search", "meetup-search.ts"),
    "eventbrite": UnsupportedConnector("Eventbrite", "eventbrite.ts"),
    "fatsoma": UnsupportedConnector("Fatsoma", "fatsoma.ts"),
}
