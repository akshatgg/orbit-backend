"""Connector registry: one entry per `event_sources.kind` (the TypeScript `CONNECTORS`)."""

from typing import Any

from .eventbrite import EventbriteConnector
from .fatsoma import FatsomaConnector
from .ical import IcalConnector
from .luma import LumaDiscoverConnector
from .meetup import MeetupSearchConnector

CONNECTORS: dict[str, Any] = {
    "ical": IcalConnector(),
    "luma": IcalConnector(),
    "luma-discover": LumaDiscoverConnector(),
    "meetup": IcalConnector(),
    "meetup-search": MeetupSearchConnector(),
    "eventbrite": EventbriteConnector(),
    "fatsoma": FatsomaConnector(),
}
