"""Pure rules for reading event pages: which events, when to stop, what to fill
(port of `enrich-plan.ts`)."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from app.connectors import js
from app.connectors.platforms import luma_page_url
from app.discovery.places import normalise_country, place_of

from .interests import with_interests

ENRICH_LIMIT = 25  # pages per source per run
MAX_ATTEMPTS = 3
RETRY_AFTER_MS = 24 * 60 * 60 * 1000
ENRICH_BUDGET_MS = 120_000  # per source; the run-wide deadline keeps a refresh bounded
PAUSE_MS = 1000  # between page reads
PAGE_WORST_CASE_MS = 40_000  # one pause + page fetch + Cloudinary import
RUN_ENRICH_DEADLINE_MS = 200_000  # page reading for a whole refresh stops by then

# Luma pages read before this were saved with a flattened description: read them once more.
FORMATTED_DESCRIPTIONS_SINCE = js.parse_date("2026-10-24T00:00:00Z")
# Pages read before countries were saved are read once more for it.
COUNTRIES_SINCE = js.parse_date("2026-10-23T00:00:00Z")


@dataclass
class RunState:
    """Shared by every source in one refresh: the time limit (epoch ms), and whether a platform
    has blocked page reads."""

    deadline: int
    blocked: bool = False


def has_time_for(now_ms: int, deadline_ms: int) -> bool:
    """Is there room for one more page, even in its worst case, before the deadline?"""
    return now_ms + PAGE_WORST_CASE_MS <= deadline_ms


def hinted_tags(event: dict[str, Any], hints: list[dict[str, Any]] | None) -> list[str] | None:
    """The event's tags with the interests its page's hints add, or None when nothing changes."""
    if event.get("tags_locked") or not hints:
        return None
    current = list(event.get("tags") or [])
    tags = with_interests(
        {"title": event.get("title") or "", "summary": event.get("summary"), "description": event.get("description"), "tags": current},
        hints,
    )
    return tags if len(tags) > len(current) else None


def _ms(value: Any) -> int:
    moment = js.to_datetime(value)
    return js.to_ms(moment) if moment is not None else 0


def pick_events_to_enrich(
    events: list[dict[str, Any]],
    details: dict[Any, dict[str, Any]],
    now: datetime,
    limit: int = ENRICH_LIMIT,
    eligible: Callable[[dict[str, Any]], bool] | None = None,
) -> list[dict[str, Any]]:
    """Upcoming events never read, or failed ones due a retry; newest first."""
    eligible = eligible or (lambda event: bool(luma_page_url(event)))
    now_ms = js.to_ms(now)

    def due(event: dict[str, Any]) -> bool:
        if not eligible(event) or _ms(event["starts_at"]) <= now_ms:
            return False
        row = details.get(event["id"])
        if row is None:
            return True
        return (
            row["status"] == "failed"
            and row["attempts"] < MAX_ATTEMPTS
            and now_ms - _ms(row["fetched_at"]) >= RETRY_AFTER_MS
        )

    picked = [e for e in events if due(e)]
    picked.sort(key=lambda e: js.to_datetime(e["created_at"]), reverse=True)  # stable, newest first
    return picked[:limit]


def is_blocked_status(status: int) -> bool:
    """A platform is refusing or rate-limiting us: stop reading pages for this run."""
    return status in (403, 429, 503)


def needs_country(event: dict[str, Any]) -> bool:
    """Not online, no stored country, and a city Orbit doesn't recognise (or none)."""
    return event.get("format") != "online" and not event.get("country") and not place_of(event.get("city"), None)["country"]


def reread_for_country(event: dict[str, Any], fetched_at: Any) -> bool:
    return needs_country(event) and _ms(fetched_at) < COUNTRIES_SINCE


def details_patch(current: dict[str, Any], page: dict[str, Any], imported_cover: str | None) -> dict[str, Any]:
    """Only fills empty fields, so admin edits win; a still-flattened description is upgraded."""
    patch: dict[str, Any] = {}
    if not current.get("cover_url") and imported_cover:
        patch["cover_url"] = imported_cover
    description = current.get("description")
    untouched_flat = bool(description) and description == page.get("flat_description")
    if page.get("description") and description != page["description"] and (not description or untouched_flat):
        patch["description"] = page["description"]
    if current.get("price_min") is None and page.get("price_min") is not None and page.get("price_max") is not None and page.get("currency"):
        patch["price_min"] = page["price_min"]
        patch["price_max"] = page["price_max"]
        patch["currency"] = page["currency"]
    for column in ("venue_name", "address", "city"):
        if not current.get(column) and page.get(column):
            patch[column] = page[column]
    country = normalise_country(page.get("country"))
    if not current.get("country") and country:
        patch["country"] = country
    if current.get("latitude") is None and page.get("latitude") is not None and page.get("longitude") is not None:
        patch["latitude"] = page["latitude"]
        patch["longitude"] = page["longitude"]
    return patch


def imported_description(current: dict[str, Any], full: str | None) -> str | None:
    """Eventbrite's full description, when the event still has its imported one (or none)."""
    if not full or full == current.get("description"):
        return None
    return full if not current.get("description") or current.get("description") == current.get("summary") else None
