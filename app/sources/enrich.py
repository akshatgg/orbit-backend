"""Page-detail enrichment for imported events (port of `enrich.ts`, `enrich-meetup.ts` and
`enrich-imports.ts`).

Each reader reads a few event pages per run, politely (1 s apart, a per-source budget and the
refresh-wide deadline, stopping for every later source once a platform blocks us), and only
fills fields that are still empty, so an admin's edit always wins. Readers never raise:
every failure is recorded on the run (P5). Each page's writes are committed as soon as the
page is done, so a later failure never undoes them (as with the TypeScript engine).
"""

import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors import fetch as http
from app.connectors import js
from app.connectors.base import error_message
from app.connectors.eventbrite import eventbrite_id_of, parse_eventbrite_description, structured_content_url
from app.connectors.luma import parse_luma_event_page
from app.connectors.meetup import is_meetup_hosted_image, parse_meetup_event_page
from app.connectors.platforms import is_platform_hosted_image, luma_page_url, meetup_page_url
from app.events.models import Event
from app.sources import cloudinary
from app.sources.enrich_plan import (
    ENRICH_BUDGET_MS,
    ENRICH_LIMIT,
    FORMATTED_DESCRIPTIONS_SINCE,
    PAUSE_MS,
    RunState,
    details_patch,
    has_time_for,
    hinted_tags,
    imported_description,
    is_blocked_status,
    needs_country,
    pick_events_to_enrich,
    reread_for_country,
)
from app.sources.models import SourceEventDetails
from app.discovery.places import normalise_country

NO_COVER = "No cover image on the page."
NO_PRICE = "No price on the page."
PLATFORM = {"eventbrite": "Eventbrite", "fatsoma": "Fatsoma"}

_LUMA_COLUMNS = (
    "id", "source_event_id", "external_url", "starts_at", "created_at", "updated_at", "cover_url", "description",
    "price_min", "venue_name", "address", "city", "country", "format", "latitude", "longitude", "title", "summary",
    "tags", "tags_locked",
)
_MEETUP_COLUMNS = (
    "id", "source_event_id", "external_url", "starts_at", "created_at", "cover_url", "description", "price_min",
    "venue_name", "address", "city", "country", "format", "latitude", "longitude", "series_key", "title", "summary",
    "tags", "tags_locked",
)
_IMPORT_COLUMNS = ("id", "source_event_id", "external_url", "starts_at", "created_at", "cover_url", "description", "summary", "price_min")


def now_ms() -> int:
    return int(time.time() * 1000)


def db_message(exc: BaseException) -> str:
    """The database's own message (what PostgREST returned as `error.message`)."""
    orig = getattr(exc, "orig", None)
    text = str(orig) if orig is not None else error_message(exc)
    if text.startswith("<class '") and ">: " in text:
        text = text.split(">: ", 1)[1]
    return text.split("\n", 1)[0]


async def _candidates(db: AsyncSession, key: str, now: datetime, columns: tuple[str, ...]) -> list[dict[str, Any]]:
    rows = await db.execute(
        select(*(getattr(Event, c) for c in columns))
        .where(Event.source == key, Event.status == "published", Event.starts_at > now)
        .order_by(Event.created_at.desc())
        .limit(500)
    )
    out = []
    for row in rows.mappings():
        item = dict(row)
        for column in ("price_min",):
            if column in item and item[column] is not None:
                item[column] = float(item[column])
        out.append(item)
    return out


async def _details(db: AsyncSession, events: list[dict[str, Any]]) -> dict[Any, dict[str, Any]]:
    details: dict[Any, dict[str, Any]] = {}
    for i in range(0, len(events), 200):
        ids = [e["id"] for e in events[i : i + 200]]
        rows = await db.execute(select(SourceEventDetails).where(SourceEventDetails.event_id.in_(ids)))
        for row in rows.scalars():
            details[row.event_id] = {
                "event_id": row.event_id, "status": row.status, "attempts": row.attempts,
                "last_error": row.last_error, "fetched_at": row.fetched_at,
            }
    return details


class _Recorder:
    """Upserts one event's `source_event_details` row (attempts + 1, wall-clock fetched_at)."""

    def __init__(self, db: AsyncSession, errors: list[dict[str, Any]], label: str) -> None:
        self.db = db
        self.errors = errors
        self.label = label

    async def __call__(self, event: dict[str, Any], attempts: int, status: str, last_error: str | None = None) -> None:
        values = {
            "event_id": event["id"],
            "status": status,
            "attempts": attempts,
            "last_error": js.u16slice(last_error, 500) if last_error is not None else None,
            "fetched_at": datetime.now(timezone.utc),
        }
        try:
            async with self.db.begin_nested():
                await self.db.execute(
                    pg_insert(SourceEventDetails)
                    .values(**values)
                    .on_conflict_do_update(index_elements=["event_id"], set_={k: v for k, v in values.items() if k != "event_id"})
                )
        except Exception as exc:  # noqa: BLE001
            self.errors.append({"event": event.get("source_event_id") or str(event["id"]), "message": f"{self.label}: {db_message(exc)}"})


async def _guarded_update(db: AsyncSession, key: str, event_id: Any, values: dict[str, Any], *conditions: Any) -> bool:
    """One guarded write; True when it changed the row. Raises the database's message."""
    try:
        async with db.begin_nested():
            result = await db.execute(
                update(Event).where(Event.id == event_id, Event.source == key, *conditions).values(**values).returning(Event.id)
            )
            return result.first() is not None
    except Exception as exc:
        raise Exception(db_message(exc)) from exc


async def _write_tags(db: AsyncSession, key: str, event_id: Any, tags: list[str]) -> None:
    await _guarded_update(db, key, event_id, {"tags": tags}, Event.tags_locked.is_(False))


def _id_of(event: dict[str, Any]) -> str:
    return event.get("source_event_id") or str(event["id"])


# ─── Luma ─────────────────────────────────────────────────────────────────────

async def enrich_luma_events(
    db: AsyncSession, source: dict[str, Any], *, now: datetime, errors: list[dict[str, Any]], run: RunState
) -> int:
    """Reads new Luma event pages once each and fills empty cover / description / price / place."""
    if run.blocked:
        return 0
    saved = 0
    key = f"source:{source['id']}"
    try:
        try:
            events = await _candidates(db, key, now, _LUMA_COLUMNS)
            if not events:
                return 0
            details = await _details(db, events)
        except Exception as exc:  # noqa: BLE001 - don't enrich blind
            errors.append({"message": f"Page details: {db_message(exc)}"})
            return 0

        await db.commit()
        by_id = {e["id"]: e for e in events}
        for event_id, row in list(details.items()):
            event = by_id.get(event_id)
            read_before = js.to_ms(row["fetched_at"]) < FORMATTED_DESCRIPTIONS_SINCE
            if row["status"] == "ok" and event is not None and event.get("description") and read_before:
                details.pop(event_id, None)
            if row["status"] == "ok" and event is not None and reread_for_country(event, row["fetched_at"]):
                details.pop(event_id, None)

        picked = pick_events_to_enrich(events, details, now)
        limit = min(now_ms() + ENRICH_BUDGET_MS, run.deadline)
        covers_possible = cloudinary.configured()
        if not covers_possible and picked:
            errors.append({"message": "Image import needs Cloudinary (CLOUDINARY_* env vars are missing), so covers were skipped."})
        record = _Recorder(db, errors, "Couldn't record page details")

        async def apply_group(event: dict[str, Any], column: str, values: dict[str, Any]) -> bool:
            # Empty columns are guarded by "still empty"; the flattened-description upgrade (the only
            # write over a value) by the row's updated_at, so any save since we read it wins.
            if event.get(column) is None or not event.get("updated_at"):
                guard = getattr(Event, column).is_(None)
            else:
                guard = Event.updated_at == event["updated_at"]
            return await _guarded_update(db, key, event["id"], values, guard)

        for index, event in enumerate(picked):
            if run.blocked or not has_time_for(now_ms(), limit):
                break
            if index > 0:
                await http.sleep(PAUSE_MS)
            url = luma_page_url(event)
            attempts = (details.get(event["id"], {}).get("attempts") or 0) + 1
            try:
                page = parse_luma_event_page(await http.fetch_feed_text(url, accept="text/html"))
                if page is None:
                    await record(event, attempts, "failed", "No event details found on the page.")
                    continue
                cover = None
                cover_error = None
                if covers_possible and not event.get("cover_url") and page.get("cover_url"):
                    try:
                        cover = await cloudinary.import_remote_image(page["cover_url"])
                    except Exception as exc:  # noqa: BLE001
                        cover_error = error_message(exc)
                patch = details_patch(event, page, cover)
                wrote = False
                try:
                    if patch.get("description"):
                        wrote = await apply_group(event, "description", {"description": patch["description"]}) or wrote
                    if patch.get("cover_url"):
                        wrote = await apply_group(event, "cover_url", {"cover_url": patch["cover_url"]}) or wrote
                    tags = hinted_tags(event, page.get("interest_hints"))
                    if tags:
                        await _write_tags(db, key, event["id"], tags)
                    if "price_min" in patch:
                        price = {"price_min": patch["price_min"], "price_max": patch["price_max"], "currency": patch["currency"]}
                        wrote = await apply_group(event, "price_min", price) or wrote
                    for column in ("venue_name", "address", "city", "country"):
                        if patch.get(column):
                            wrote = await apply_group(event, column, {column: patch[column]}) or wrote
                    if "latitude" in patch:
                        point = {"latitude": patch["latitude"], "longitude": patch["longitude"]}
                        wrote = await apply_group(event, "latitude", point) or wrote
                except Exception as exc:  # noqa: BLE001
                    if wrote:
                        saved += 1
                    await record(event, attempts, "failed", error_message(exc))
                    continue
                if wrote:
                    saved += 1
                await record(event, attempts, "ok", cover_error)
                if cover_error:
                    errors.append({"event": _id_of(event), "message": f"Cover not imported: {cover_error}"})
            except Exception as exc:  # noqa: BLE001
                if isinstance(exc, http.FetchHttpError) and is_blocked_status(exc.status):
                    run.blocked = True
                    await record(event, attempts, "blocked", str(exc))
                    errors.append({"message": "Luma is blocking page reads. Paste or upload images for now."})
                    break
                await record(event, attempts, "failed", error_message(exc))
                errors.append({"event": _id_of(event), "message": f"Page details: {error_message(exc)}"})
            finally:
                await db.commit()
    except Exception as exc:  # noqa: BLE001
        errors.append({"message": f"Page details: {error_message(exc)}"})
    return saved


# ─── Meetup ───────────────────────────────────────────────────────────────────

def _needs_location(event: dict[str, Any]) -> bool:
    return bool(meetup_page_url(event)) and (
        not event.get("venue_name") or not event.get("address") or not event.get("city")
        or event.get("latitude") is None or event.get("longitude") is None
    )


def _needs_price(event: dict[str, Any]) -> bool:
    return bool(meetup_page_url(event)) and event.get("price_min") is None


async def enrich_meetup_events(
    db: AsyncSession, source: dict[str, Any], *, now: datetime, errors: list[dict[str, Any]], run: RunState
) -> int:
    """Reads new Meetup event pages once each for venue, address, city, coordinates, price and cover."""
    if run.blocked:
        return 0
    saved = 0
    key = f"source:{source['id']}"
    try:
        try:
            events = await _candidates(db, key, now, _MEETUP_COLUMNS)
        except Exception as exc:  # noqa: BLE001
            errors.append({"message": f"Meetup page details: {db_message(exc)}"})
            return 0
        if not events:
            return 0

        covers_possible = cloudinary.configured()
        # Every date of a series has the same poster: import it once and share Orbit's copy.
        series_cover: dict[str, str] = {}
        for e in events:
            if e.get("series_key") and e.get("cover_url") and not is_meetup_hosted_image(e["cover_url"]):
                series_cover[e["series_key"]] = e["cover_url"]

        def needs_cover(event: dict[str, Any]) -> bool:
            return covers_possible and bool(meetup_page_url(event)) and (
                not event.get("cover_url") or is_meetup_hosted_image(event["cover_url"])
            )

        try:
            details = await _details(db, events)
        except Exception as exc:  # noqa: BLE001
            errors.append({"message": f"Meetup page details: {db_message(exc)}"})
            return 0

        await db.commit()
        by_id = {e["id"]: e for e in events}
        for event_id, row in list(details.items()):
            event = by_id.get(event_id)
            if event is None or row["status"] != "ok":
                continue
            last_error = row.get("last_error") or ""
            cover_pending = needs_cover(event) and NO_COVER not in last_error
            price_pending = _needs_price(event) and NO_PRICE not in last_error
            country_pending = reread_for_country(event, row["fetched_at"])
            if _needs_location(event) or cover_pending or price_pending or country_pending:
                details.pop(event_id, None)

        def eligible(event: dict[str, Any]) -> bool:
            # needsCountry alone can pick an event with no page URL in TS (it then fails to fetch);
            # only events with a Meetup page are read here.
            return bool(meetup_page_url(event)) and (
                _needs_location(event) or needs_cover(event) or _needs_price(event) or needs_country(event)
            )

        picked = pick_events_to_enrich(events, details, now, ENRICH_LIMIT, eligible)
        limit = min(now_ms() + ENRICH_BUDGET_MS, run.deadline)
        record = _Recorder(db, errors, "Couldn't record location")

        for index, event in enumerate(picked):
            if run.blocked or not has_time_for(now_ms(), limit):
                break
            if index > 0:
                await http.sleep(PAUSE_MS)
            url = meetup_page_url(event)
            attempts = (details.get(event["id"], {}).get("attempts") or 0) + 1
            try:
                page = parse_meetup_event_page(await http.fetch_feed_text(url, accept="text/html"))
                if page is None:
                    await record(event, attempts, "failed", "No location, cover or price found on the page.")
                    continue

                cover = None
                cover_error = None
                shared = series_cover.get(event["series_key"]) if event.get("series_key") else None
                if needs_cover(event) and shared:
                    cover = shared
                elif needs_cover(event) and page.get("cover_url"):
                    try:
                        cover = await cloudinary.import_remote_image(page["cover_url"])
                        if event.get("series_key"):
                            series_cover[event["series_key"]] = cover
                    except Exception as exc:  # noqa: BLE001
                        cover_error = f"Cover not imported: {error_message(exc)}"

                async def apply(values: dict[str, Any], column: str, current: Any, event: dict[str, Any] = event) -> bool:
                    guard = getattr(Event, column).is_(None) if current is None else getattr(Event, column) == current
                    return await _guarded_update(db, key, event["id"], values, guard)

                wrote = False
                if not event.get("venue_name") and page.get("venue_name"):
                    wrote = await apply({"venue_name": page["venue_name"]}, "venue_name", event.get("venue_name")) or wrote
                if not event.get("address") and page.get("address"):
                    wrote = await apply({"address": page["address"]}, "address", event.get("address")) or wrote
                if (not event.get("city") or event.get("city") == "London") and page.get("city") and page["city"] != event.get("city"):
                    wrote = await apply({"city": page["city"]}, "city", event.get("city")) or wrote
                country = normalise_country(page.get("country"))
                if not event.get("country") and country:
                    wrote = await apply({"country": country}, "country", event.get("country")) or wrote
                if (event.get("latitude") is None or event.get("longitude") is None) and page.get("latitude") is not None and page.get("longitude") is not None:
                    wrote = await apply(
                        {"latitude": page["latitude"], "longitude": page["longitude"]}, "latitude", event.get("latitude")
                    ) or wrote
                if cover:
                    wrote = await apply({"cover_url": cover}, "cover_url", event.get("cover_url")) or wrote
                if _needs_price(event) and page.get("price"):
                    amount, currency = page["price"]["amount"], page["price"]["currency"]
                    values = {"price_min": amount, "price_max": amount, **({"currency": currency} if currency else {})}
                    wrote = await apply(values, "price_min", event.get("price_min")) or wrote
                tags = hinted_tags(event, page.get("interest_hints"))
                if tags:
                    await _write_tags(db, key, event["id"], tags)
                if wrote:
                    saved += 1
                if cover_error:
                    # "failed" so the retry limits bring the image back on a later refresh.
                    await record(event, attempts, "failed", cover_error)
                    errors.append({"event": _id_of(event), "message": cover_error})
                else:
                    missing = [
                        m for m in (
                            needs_cover(event) and not page.get("cover_url") and NO_COVER,
                            _needs_price(event) and not page.get("price") and NO_PRICE,
                        ) if m
                    ]
                    await record(event, attempts, "ok", " ".join(missing) or None)
            except Exception as exc:  # noqa: BLE001
                if isinstance(exc, http.FetchHttpError) and is_blocked_status(exc.status):
                    run.blocked = True
                    await record(event, attempts, "blocked", str(exc))
                    errors.append({"message": "Meetup is blocking page reads. We'll try again on the next refresh."})
                    break
                await record(event, attempts, "failed", error_message(exc))
                errors.append({"event": _id_of(event), "message": f"Meetup page details: {error_message(exc)}"})
            finally:
                await db.commit()

        # Every date of a series has the same poster: give dates still without one the series' cover.
        for series_key, cover in series_cover.items():
            try:
                async with db.begin_nested():
                    await db.execute(
                        update(Event)
                        .where(
                            Event.source == key,
                            Event.series_key == series_key,
                            or_(
                                Event.cover_url.is_(None),
                                Event.cover_url.like("https://secure.meetupstatic.com/%"),
                                Event.cover_url.like("https://secure-content.meetupstatic.com/%"),
                            ),
                        )
                        .values(cover_url=cover)
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append({"message": f"Couldn't share a series cover: {db_message(exc)}"})
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        errors.append({"message": f"Meetup page details: {error_message(exc)}"})
    return saved


# ─── Eventbrite and Fatsoma ───────────────────────────────────────────────────

async def enrich_imported_events(
    db: AsyncSession, source: dict[str, Any], *, now: datetime, errors: list[dict[str, Any]], run: RunState
) -> int:
    """Eventbrite's full description, and for both platforms Orbit's own copy of a hotlinked cover."""
    kind = source["kind"]
    platform = PLATFORM[kind]
    reads_pages = kind == "eventbrite"
    if reads_pages and run.blocked:
        return 0
    saved = 0
    key = f"source:{source['id']}"
    try:
        try:
            events = await _candidates(db, key, now, _IMPORT_COLUMNS)
        except Exception as exc:  # noqa: BLE001
            errors.append({"message": f"{platform} details: {db_message(exc)}"})
            return 0
        if not events:
            return 0

        covers_possible = cloudinary.configured()

        def hotlinked(event: dict[str, Any]) -> bool:
            return is_platform_hosted_image(kind, event.get("cover_url"))

        def needs_cover(event: dict[str, Any]) -> bool:
            return covers_possible and hotlinked(event)

        def needs_read(event: dict[str, Any]) -> bool:
            return reads_pages and eventbrite_id_of(event.get("source_event_id")) is not None

        if not covers_possible and any(hotlinked(e) for e in events):
            errors.append({
                "message": f"Covers stay linked to {platform}: image import needs Cloudinary (CLOUDINARY_* env vars are missing)."
            })

        try:
            details = await _details(db, events)
        except Exception as exc:  # noqa: BLE001
            errors.append({"message": f"{platform} details: {db_message(exc)}"})
            return 0
        await db.commit()
        by_id = {e["id"]: e for e in events}
        for event_id, row in list(details.items()):
            event = by_id.get(event_id)
            if row["status"] == "ok" and event is not None and needs_cover(event):
                details.pop(event_id, None)

        picked = pick_events_to_enrich(events, details, now, ENRICH_LIMIT, lambda e: needs_read(e) or needs_cover(e))
        limit = min(now_ms() + ENRICH_BUDGET_MS, run.deadline)
        record = _Recorder(db, errors, "Couldn't record details")

        for index, event in enumerate(picked):
            if (reads_pages and run.blocked) or not has_time_for(now_ms(), limit):
                break
            if index > 0:
                await http.sleep(PAUSE_MS)
            attempts = (details.get(event["id"], {}).get("attempts") or 0) + 1

            async def apply(column: str, value: str, current: str | None, event: dict[str, Any] = event) -> bool:
                guard = getattr(Event, column).is_(None) if current is None else getattr(Event, column) == current
                return await _guarded_update(db, key, event["id"], {column: value}, guard)

            try:
                wrote = False
                eventbrite_id = eventbrite_id_of(event.get("source_event_id")) if needs_read(event) else None
                if eventbrite_id:
                    body = await http.fetch_feed_text(structured_content_url(eventbrite_id), accept="application/json")
                    description = imported_description(event, parse_eventbrite_description(body))
                    if description:
                        wrote = await apply("description", description, event.get("description")) or wrote
                cover_error = None
                if needs_cover(event):
                    try:
                        cover = await cloudinary.import_remote_image(event["cover_url"])
                        wrote = await apply("cover_url", cover, event.get("cover_url")) or wrote
                    except Exception as exc:  # noqa: BLE001
                        cover_error = f"Cover not imported: {error_message(exc)}"
                if wrote:
                    saved += 1
                if cover_error:
                    await record(event, attempts, "failed", cover_error)
                    errors.append({"event": _id_of(event), "message": cover_error})
                else:
                    await record(event, attempts, "ok")
            except Exception as exc:  # noqa: BLE001
                if isinstance(exc, http.FetchHttpError) and is_blocked_status(exc.status):
                    run.blocked = True
                    # "failed", not "blocked": the retry rules read it again after a day.
                    await record(event, attempts, "failed", f"Blocked by {platform}: {exc}")
                    errors.append({"message": f"{platform} is blocking detail reads. We'll try again on the next refresh."})
                    break
                await record(event, attempts, "failed", error_message(exc))
                errors.append({"event": _id_of(event), "message": f"{platform} details: {error_message(exc)}"})
            finally:
                await db.commit()
    except Exception as exc:  # noqa: BLE001
        errors.append({"message": f"{platform} details: {error_message(exc)}"})
    return saved
