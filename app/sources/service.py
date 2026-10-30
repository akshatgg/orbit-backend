"""Source sync engine: fetch → plan → apply, plus tagging, covers, page details, expiry and
duplicate detection.

A port of Orbit's TypeScript engine (`src/lib/sources/engine.ts` and `sync-plan.ts`). Orbit
never auto-cancels: an event missing or cancelled upstream is only flagged for review, and
only on the run where it first goes missing.

The engine works in its own sessions, not a request's: every transaction it opens marks the
connection as the trusted writer (`app.trusted`, see is_service()), and work is committed as
it goes (the sync's writes, then tags and covers, then each page read), so partial progress
survives a later failure exactly as it did with the TypeScript engine.
"""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Iterable, Sequence
from uuid import UUID

from sqlalchemy import event as sa_event
from sqlalchemy import insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.categories.models import Category
from app.connectors import js, urls
from app.connectors.base import FetchedFeed, error_message
from app.connectors.registry import CONNECTORS
from app.connectors.timezone import local_day
from app.core.database import AsyncSessionLocal, engine
from app.events.models import Event
from app.cron.autopost import run_autopost
from app.sources.classify import classify_event
from app.sources.duplicates import certain_duplicate, find_duplicates, pair_key
from app.sources.enrich import (
    db_message,
    enrich_imported_events,
    enrich_luma_events,
    enrich_meetup_events,
)
from app.sources.enrich_plan import ENRICH_BUDGET_MS, RUN_ENRICH_DEADLINE_MS, RunState
from app.sources.interests import with_interests
from app.sources.models import EventDuplicateDismissal, EventSource, SourceRun
from app.discovery.places import normalise_country
from app.sources.repeats import group_repeats

logger = logging.getLogger(__name__)

PAGE = 1000
CHUNK = 200
DAY_MS = 24 * 60 * 60 * 1000

SYNCED_FIELDS: tuple[str, ...] = (
    "title", "summary", "description", "starts_at", "ends_at", "timezone", "format", "venue_name", "address",
    "city", "country", "latitude", "longitude", "registration_mode", "external_url", "source_url", "series_key",
    "series_label",
)

# Synced fields a kind's feed doesn't really have, so updates leave them alone.
SKIPPED_FIELDS: dict[str, tuple[str, ...]] = {
    "luma": ("description", "city"),
    "luma-discover": ("description", "city"),
    "meetup": ("venue_name", "address", "city", "latitude", "longitude"),
    "meetup-search": ("venue_name", "address", "city", "latitude", "longitude"),
    "eventbrite": ("description",),
}

# Location fields Luma page reads fill in when the listing hides the place until you register.
# A later sync never empties them again: only a real (non-empty) value from the listing replaces
# what's stored. Deliberate divergence from the TypeScript engine (approved by Akshat), which
# nulled them on every run so the same events were rewritten nightly.
KEEP_FILLED_FIELDS: dict[str, tuple[str, ...]] = {
    "luma": ("venue_name", "address", "latitude", "longitude"),
    "luma-discover": ("venue_name", "address", "latitude", "longitude"),
}

# Kinds that read a platform's search or listing rather than a full calendar. Events drop out
# of those lists while their pages stay up, so a run only adds and updates: it never flags an
# event as missing (a cancellation the platform reports is still flagged).
ADD_ONLY_KINDS = {"luma-discover", "meetup-search", "eventbrite", "fatsoma"}

EXTERNAL_LABELS: dict[str, str] = {
    "ical": "View details",
    "luma": "Register on Luma",
    "luma-discover": "Register on Luma",
    "meetup": "Register on Meetup",
    "meetup-search": "Register on Meetup",
    "eventbrite": "Register on Eventbrite",
    "fatsoma": "Get tickets on Fatsoma",
}

LIVE_STATUSES = {"published", "draft"}

EXISTING_COLUMNS = (
    Event.id, Event.source_event_id, Event.title, Event.summary, Event.description, Event.starts_at, Event.ends_at,
    Event.timezone, Event.format, Event.venue_name, Event.address, Event.city, Event.country, Event.latitude,
    Event.longitude, Event.registration_mode, Event.external_url, Event.source_url, Event.series_key,
    Event.series_label, Event.status, Event.needs_review, Event.duplicate_of, Event.last_verified_at,
)


def source_key(source_id: UUID | str) -> str:
    """The `events.source` value for events imported from this source."""
    return f"source:{source_id}"


def source_id_from_key(source: str | None) -> UUID | None:
    if not source or not source.startswith("source:"):
        return None
    try:
        return UUID(source.split(":", 1)[1])
    except ValueError:
        return None


def _ms(value: Any) -> int | None:
    moment = js.to_datetime(value)
    return js.to_ms(moment) if moment is not None else None


# ─── Sync plan (sync-plan.ts) ─────────────────────────────────────────────────

def provenance_url(event: dict[str, Any], feed_url: str) -> str | None:
    """The event's own page, else the feed's site (never the full feed URL, which can hold a token)."""
    if event.get("url"):
        return event["url"]
    parsed = urls.parse(feed_url or "")
    return parsed.origin if parsed is not None else None


def synced_fields(event: dict[str, Any], feed_url: str) -> dict[str, Any]:
    """The fields a source owns on its events. `country` only when the feed names a real one."""
    country = normalise_country(event.get("country"))
    fields: dict[str, Any] = {
        "title": event["title"],
        "summary": event.get("summary"),
        "description": event.get("description"),
        "starts_at": event["starts_at"],
        "ends_at": event.get("ends_at"),
        "timezone": event["timezone"],
        "format": event["format"],
        "venue_name": event.get("venue_name"),
        "address": event.get("address"),
        "city": event.get("city"),
    }
    if country:
        fields["country"] = country
    fields.update({
        "latitude": event.get("latitude"),
        "longitude": event.get("longitude"),
        "registration_mode": "external" if event.get("url") else "none",
        "external_url": event.get("url"),
        "source_url": provenance_url(event, feed_url),
        "series_key": event.get("series_key"),
        "series_label": event.get("series_label"),
    })
    return fields


def build_insert_row(
    event: dict[str, Any], source: dict[str, Any], now: datetime, category_id: Any = ...
) -> dict[str, Any]:
    """Row for an event Orbit hasn't seen before. The platform's price is only set here."""
    row = synced_fields(event, source["url"])
    row.setdefault("country", None)  # every row of a multi-row insert carries the same columns
    price = event.get("price")
    row.update({
        "source": source_key(source["id"]),
        "source_event_id": event["source_event_id"],
        "external_label": EXTERNAL_LABELS.get(source.get("kind") or "ical", "View details"),
        "organiser_id": source.get("default_organiser_id"),
        "category_id": source.get("default_category_id") if category_id is ... else category_id,
        "tags": list(source.get("default_tags") or []),
        "price_min": price["min"] if price else None,
        "price_max": price["max"] if price else None,
        "currency": price["currency"] if price else "GBP",  # the column default
        "status": "published",
        "last_verified_at": now,
    })
    return row


def _same_instant(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return _ms(a) == _ms(b)


def _same_coordinate(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(js.number(a) - js.number(b)) < 1e-6


def changed_fields(
    existing: dict[str, Any], incoming: dict[str, Any], skip: Iterable[str] = (), keep_filled: Iterable[str] = ()
) -> dict[str, Any]:
    """Only the fields that actually changed (empty when nothing did). `keep_filled` fields are
    never emptied: an empty incoming value leaves a stored one alone."""
    skipped = set(skip)
    kept = set(keep_filled)
    nxt = {k: v for k, v in incoming.items() if k not in skipped}
    if existing.get("registration_mode") == "tickets":
        # An admin switched this event to Orbit tickets: leave registration alone.
        nxt.pop("registration_mode", None)
        nxt.pop("external_url", None)
    patch: dict[str, Any] = {}
    for key, value in nxt.items():
        current = existing.get(key)
        if key in kept and (value is None or value == "") and current is not None and current != "":
            continue
        if key in ("starts_at", "ends_at"):
            same = _same_instant(current, value)
        elif key in ("latitude", "longitude"):
            same = _same_coordinate(current, value)
        else:
            same = current == value and type(current) is type(value) or (current is None and value is None)
        if not same:
            patch[key] = value
    return patch


def plan_sync(
    existing: Sequence[dict[str, Any]],
    incoming: Sequence[dict[str, Any]],
    *,
    now: datetime,
    feed_url: str,
    previous_run_started_at: datetime | None,
    skip: Iterable[str] = (),
    keep_filled: Iterable[str] = (),
    coverage: str | dict[str, Any] = "complete",
) -> dict[str, list[Any]]:
    """Works out inserts / updates / unchanged / flags; events are matched by source_event_id."""
    plan: dict[str, list[Any]] = {"inserts": [], "updates": [], "unchanged": [], "flagged": [], "unflagged": []}
    now_ms = js.to_ms(now)
    by_id = {e["source_event_id"]: e for e in existing if e.get("source_event_id")}
    seen_ids: set[str] = set()
    # A series date can change id upstream (Meetup gives unannounced dates a temporary id and a
    # numeric one later). Same series + same start is the same date: re-key it.
    incoming_ids = {e["source_event_id"] for e in incoming}

    def series_date(key: str, starts_at: Any) -> str:
        return f"{key}|{_ms(starts_at)}"

    by_series_date = {
        series_date(e["series_key"], e["starts_at"]): e
        for e in existing
        if e.get("series_key") and e.get("source_event_id") and e["source_event_id"] not in incoming_ids
    }
    previous_ms = js.to_ms(previous_run_started_at) if previous_run_started_at else None

    def was_seen_last_run(e: dict[str, Any]) -> bool:
        return previous_ms is None or (_ms(e["last_verified_at"]) or 0) >= previous_ms

    def flag(e: dict[str, Any], reason: str) -> None:
        if not e.get("needs_review") and e.get("status") in LIVE_STATUSES and was_seen_last_run(e):
            plan["flagged"].append({"id": e["id"], "reason": reason})

    for event in incoming:
        if event["source_event_id"] in seen_ids:
            continue
        seen_ids.add(event["source_event_id"])
        current = by_id.get(event["source_event_id"])
        rekeyed = False
        if current is None and event.get("series_key"):
            key = series_date(event["series_key"], event["starts_at"])
            current = by_series_date.get(key)
            if current is not None:
                rekeyed = True
                seen_ids.add(current["source_event_id"])
                by_series_date.pop(key, None)
        if event.get("cancelled"):
            if current is not None:
                flag(current, "cancelled")
            continue
        if current is None:
            plan["inserts"].append(event)
            continue
        # Flagged as missing or cancelled, but the source lists it again (as live): nothing to decide.
        if current.get("needs_review") and not current.get("duplicate_of"):
            plan["unflagged"].append(current["id"])
        patch = changed_fields(current, synced_fields(event, feed_url), skip, keep_filled)
        if rekeyed:
            patch["source_event_id"] = event["source_event_id"]
        if patch:
            plan["updates"].append({"id": current["id"], "source_event_id": event["source_event_id"], "patch": patch})
        else:
            plan["unchanged"].append(current["id"])

    # A flag on an event that has started is moot: there's nothing left to keep or take down.
    unflagged = set(plan["unflagged"])
    for e in existing:
        if e.get("needs_review") and not e.get("duplicate_of") and e["id"] not in unflagged and (_ms(e["starts_at"]) or 0) <= now_ms:
            plan["unflagged"].append(e["id"])

    if coverage == "sample":
        return plan
    covered_until = _ms(coverage["until"]) if isinstance(coverage, dict) else None
    for e in existing:
        if not e.get("source_event_id") or e["source_event_id"] in seen_ids:
            continue
        starts = _ms(e["starts_at"]) or 0
        if starts <= now_ms:
            continue  # past events are expected to drop off
        if covered_until is not None and starts >= covered_until:
            continue  # beyond what this fetch read
        # Platforms drop an event from their listings on its own day: not news.
        if local_day(starts, e.get("timezone")) == local_day(now_ms, e.get("timezone")):
            if e.get("needs_review") and not e.get("duplicate_of") and e["id"] not in unflagged:
                plan["unflagged"].append(e["id"])
            continue
        flag(e, "missing")
    return plan


# ─── Sessions ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def engine_session() -> AsyncIterator[AsyncSession]:
    """A session whose every transaction is the trusted writer (`app.trusted`), so the event
    guards treat the engine as the service after each commit too (pitfall P1)."""
    async with AsyncSessionLocal() as session:

        @sa_event.listens_for(session.sync_session, "after_begin")
        def _trust(_session: Any, _transaction: Any, connection: Any) -> None:
            connection.exec_driver_sql("select set_config('app.trusted', 'on', true)")

        yield session


def _db_value(key: str, value: Any) -> Any:
    if key in ("starts_at", "ends_at", "last_verified_at"):
        return js.to_datetime(value)
    return value


def _db_row(row: dict[str, Any]) -> dict[str, Any]:
    out = {k: _db_value(k, v) for k, v in row.items()}
    for key in ("organiser_id", "category_id"):
        if isinstance(out.get(key), str):
            out[key] = UUID(out[key])
    return out


def _chunks(items: Sequence[Any], size: int = CHUNK) -> Iterable[Sequence[Any]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


async def _previous_ok_run(db: AsyncSession, source_id: UUID) -> datetime | None:
    return (
        await db.execute(
            select(SourceRun.started_at)
            .where(SourceRun.source_id == source_id, SourceRun.status == "ok")
            .order_by(SourceRun.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _existing(db: AsyncSession, key: str) -> list[dict[str, Any]]:
    rows = (await db.execute(select(*EXISTING_COLUMNS).where(Event.source == key).order_by(Event.id))).mappings()
    return [dict(row) for row in rows]


async def _savepoint(db: AsyncSession, statement: Any) -> Any:
    async with db.begin_nested():
        return await db.execute(statement)


# ─── Tags and covers (engine.ts) ──────────────────────────────────────────────

async def tag_interests(
    db: AsyncSession, key: str, errors: list[dict[str, Any]], *, source_tags: list[str], hints: dict[str, list]
) -> None:
    """Adds the source's own tags and the interests each event is about. Only adds, never touches
    an event whose tags an admin edited (tags_locked). Never raises."""
    try:
        rows = (
            await db.execute(
                select(Event.id, Event.source_event_id, Event.title, Event.summary, Event.description, Event.tags, Event.tags_locked)
                .where(Event.source == key)
                .order_by(Event.id)
            )
        ).mappings().all()
    except Exception as exc:  # noqa: BLE001
        errors.append({"message": f"Couldn't load events to tag interests: {db_message(exc)}"})
        return
    for row in rows:
        if row["tags_locked"]:
            continue
        current = list(row["tags"] or [])
        with_source = [*current, *[t for t in source_tags if t not in current]]
        tags = with_interests({**row, "tags": with_source}, hints.get(row["source_event_id"] or "", []))
        if len(tags) == len(current):
            continue
        try:
            await _savepoint(db, update(Event).where(Event.id == row["id"], Event.source == key).values(tags=tags))
        except Exception as exc:  # noqa: BLE001
            errors.append({"event": str(row["id"]), "message": f"Couldn't tag interests: {db_message(exc)}"})


async def fill_covers(db: AsyncSession, key: str, events: list[dict[str, Any]], errors: list[dict[str, Any]]) -> None:
    """Gives events without a cover the platform's own image from this fetch. Never raises."""
    images = {e["source_event_id"]: e["image_url"] for e in events if e.get("image_url")}
    if not images:
        return
    try:
        bare = (
            await db.execute(
                select(Event.id, Event.source_event_id).where(Event.source == key, Event.cover_url.is_(None)).order_by(Event.id)
            )
        ).all()
    except Exception as exc:  # noqa: BLE001
        errors.append({"message": f"Couldn't load events to add images: {db_message(exc)}"})
        return
    for event_id, source_event_id in bare:
        image = images.get(source_event_id or "")
        if not image:
            continue
        try:
            await _savepoint(
                db,
                update(Event).where(Event.id == event_id, Event.source == key, Event.cover_url.is_(None)).values(cover_url=image),
            )
        except Exception as exc:  # noqa: BLE001
            errors.append({"event": str(event_id), "message": f"Couldn't add the image: {db_message(exc)}"})


# ─── One source ───────────────────────────────────────────────────────────────

def _source_dict(row: EventSource) -> dict[str, Any]:
    return {
        "id": row.id, "name": row.name, "kind": row.kind, "url": row.url,
        "default_organiser_id": row.default_organiser_id, "default_category_id": row.default_category_id,
        "default_tags": list(row.default_tags or []), "permission_confirmed": row.permission_confirmed,
        "auto_category": row.auto_category,
    }


async def run_source(
    source_id: UUID,
    trigger: str = "manual",
    *,
    now: datetime | None = None,
    run: RunState | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Fetch one source and apply the diff. Never raises: failures are recorded on the run.

    `dry_run` fetches and plans only (no writes, no page reads, no run row) and returns the
    plan, for checking what a run would change.
    """
    now = now or datetime.now(timezone.utc)
    run = run or RunState(deadline=int(time.time() * 1000) + ENRICH_BUDGET_MS)
    errors: list[dict[str, Any]] = []
    summary: dict[str, Any] = {
        "source_id": str(source_id), "name": "", "status": "error", "found": 0, "inserted": 0, "updated": 0,
        "unchanged": 0, "flagged_missing": 0, "enriched": 0, "errors": errors,
    }
    async with engine_session() as db:
        row = (await db.execute(select(EventSource).where(EventSource.id == source_id))).scalar_one_or_none()
        if row is None:
            errors.append({"message": "Source not found"})
            return summary
        source = _source_dict(row)
        summary["name"] = source["name"]
        await db.commit()

        try:
            if not source["permission_confirmed"]:
                raise Exception("Permission to use this feed hasn't been confirmed.")
            connector = CONNECTORS.get(source["kind"])
            if connector is None:
                raise Exception(f'No connector for "{source["kind"]}" sources.')

            previous_run_started_at = await _previous_ok_run(db, source["id"])
            await db.commit()  # no transaction stays open while the platform is read
            fetched: FetchedFeed = await connector.fetch(source, now)
            errors.extend(fetched.errors)
            summary["found"] = len(fetched.events)

            key = source_key(source["id"])
            # Same title at the same place on several dates = one series, even when the platform doesn't say so.
            events = group_repeats(fetched.events, key)
            existing = await _existing(db, key)
            plan = plan_sync(
                existing, events, now=now, feed_url=source["url"], previous_run_started_at=previous_run_started_at,
                skip=SKIPPED_FIELDS.get(source["kind"], ()), keep_filled=KEEP_FILLED_FIELDS.get(source["kind"], ()),
                # A search or listing only adds and updates: events that drop out of it are still on the platform.
                coverage="sample" if source["kind"] in ADD_ONLY_KINDS else fetched.coverage,
            )

            # Discovery feeds bring unrelated events: classify each new one by its own words,
            # falling back to the source's default category when nothing is clear.
            category_for: dict[str, Any] = {}
            classified = 0
            if source["auto_category"] and plan["inserts"]:
                try:
                    rules = [
                        {"id": str(r.id), "name": r.name, "keywords": list(r.keywords or [])}
                        for r in (await db.execute(select(Category).order_by(Category.sort_order))).scalars()
                    ]
                except Exception as exc:  # noqa: BLE001
                    errors.append({"message": f"Couldn't load category keywords: {db_message(exc)}"})
                    rules = []
                for event in plan["inserts"]:
                    match = classify_event(event, rules)["category_id"]
                    if match:
                        classified += 1
                    category_for[event["source_event_id"]] = UUID(match) if match else source["default_category_id"]
            summary["classified"] = classified

            if dry_run:
                summary["status"] = "ok"
                summary["plan"] = {
                    "inserts": [e["source_event_id"] for e in plan["inserts"]],
                    "updates": [{"id": str(u["id"]), "source_event_id": u["source_event_id"], "fields": sorted(u["patch"]), "patch": u["patch"]} for u in plan["updates"]],
                    "unchanged": len(plan["unchanged"]),
                    "flagged": [{"id": str(f["id"]), "reason": f["reason"]} for f in plan["flagged"]],
                    "unflagged": [str(i) for i in plan["unflagged"]],
                    "existing": len(existing),
                    "coverage": fetched.coverage,
                }
                summary["events"] = events
                return summary

            await _apply_plan(db, source, key, plan, category_for, now, summary, errors)
            await db.commit()

            hints = {e["source_event_id"]: e.get("interest_hints") or [] for e in events}
            await tag_interests(db, key, errors, source_tags=source["default_tags"], hints=hints)
            await fill_covers(db, key, events, errors)
            await db.commit()

            if source["kind"] in ("luma", "luma-discover"):
                summary["enriched"] = await enrich_luma_events(db, source, now=now, errors=errors, run=run)
            elif source["kind"] in ("meetup", "meetup-search"):
                summary["enriched"] = await enrich_meetup_events(db, source, now=now, errors=errors, run=run)
            elif source["kind"] in ("eventbrite", "fatsoma"):
                summary["enriched"] = await enrich_imported_events(db, source, now=now, errors=errors, run=run)
            await db.commit()
            summary["status"] = "ok"
        except Exception as exc:  # noqa: BLE001 - the run records the failure
            logger.warning("Source %s failed: %s", source_id, exc)
            await db.rollback()
            summary["status"] = "error"
            errors.insert(0, {"message": error_message(exc)})

        finished_at = datetime.now(timezone.utc)
        try:
            await db.execute(
                insert(SourceRun).values(
                    source_id=source["id"], trigger=trigger if trigger in ("cron", "manual") else "manual",
                    started_at=now, finished_at=finished_at, status=summary["status"], found=summary["found"],
                    inserted=summary["inserted"], updated=summary["updated"], unchanged=summary["unchanged"],
                    flagged_missing=summary["flagged_missing"], enriched=summary["enriched"], errors=errors[:50],
                )
            )
            await db.execute(
                update(EventSource)
                .where(EventSource.id == source["id"])
                .values(
                    last_run_at=finished_at,
                    last_status=summary["status"],
                    last_error=((errors[0].get("message") if errors else None) or "Unknown error")
                    if summary["status"] == "error" else None,
                )
            )
            await db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Couldn't record the run of source %s", source_id)
            await db.rollback()
            errors.append({"message": f"Couldn't record the run: {db_message(exc)}"})
    return summary


async def _apply_plan(
    db: AsyncSession,
    source: dict[str, Any],
    key: str,
    plan: dict[str, list[Any]],
    category_for: dict[str, Any],
    now: datetime,
    summary: dict[str, Any],
    errors: list[dict[str, Any]],
) -> None:
    # New events, a batch at a time; if a batch fails, retry row by row to isolate bad data.
    for batch in _chunks(plan["inserts"]):
        rows = [
            _db_row(build_insert_row(e, source, now, category_for[e["source_event_id"]] if e["source_event_id"] in category_for else ...))
            for e in batch
        ]
        try:
            await _savepoint(db, insert(Event).values(rows))
            summary["inserted"] += len(rows)
            continue
        except Exception:  # noqa: BLE001
            pass
        for event, row in zip(batch, rows):
            try:
                await _savepoint(db, insert(Event).values(**row))
                summary["inserted"] += 1
            except Exception as exc:  # noqa: BLE001
                errors.append({"event": event["source_event_id"], "message": f"Not imported: {db_message(exc)}"})

    for change in plan["updates"]:
        values = {k: _db_value(k, v) for k, v in change["patch"].items()}
        values["last_verified_at"] = now
        try:
            await _savepoint(db, update(Event).where(Event.id == change["id"], Event.source == key).values(**values))
            summary["updated"] += 1
        except Exception as exc:  # noqa: BLE001
            errors.append({"event": change["source_event_id"], "message": f"Not updated: {db_message(exc)}"})

    for ids in _chunks(plan["unchanged"]):
        try:
            await _savepoint(db, update(Event).where(Event.id.in_(ids), Event.source == key).values(last_verified_at=now))
            summary["unchanged"] += len(ids)
        except Exception as exc:  # noqa: BLE001
            errors.append({"message": f"Couldn't mark events verified: {db_message(exc)}"})

    for ids in _chunks([f["id"] for f in plan["flagged"]]):
        try:
            await _savepoint(db, update(Event).where(Event.id.in_(ids), Event.source == key).values(needs_review=True))
            summary["flagged_missing"] += len(ids)
        except Exception as exc:  # noqa: BLE001
            errors.append({"message": f"Couldn't flag missing events: {db_message(exc)}"})

    for ids in _chunks(plan["unflagged"]):
        try:
            await _savepoint(
                db,
                update(Event).where(Event.id.in_(ids), Event.source == key, Event.duplicate_of.is_(None)).values(needs_review=False),
            )
        except Exception as exc:  # noqa: BLE001
            errors.append({"message": f"Couldn't clear review flags: {db_message(exc)}"})


# ─── Expiry and duplicates ────────────────────────────────────────────────────

async def expire_events(db: AsyncSession, now: datetime | None = None) -> int:
    """Published events that ended more than a day ago become "expired"."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=1)
    ended = await db.execute(
        update(Event).where(Event.status == "published", Event.ends_at < cutoff).values(status="expired")
    )
    no_end = await db.execute(
        update(Event)
        .where(Event.status == "published", Event.ends_at.is_(None), Event.starts_at < cutoff)
        .values(status="expired")
    )
    return (ended.rowcount or 0) + (no_end.rowcount or 0)


_CANDIDATE_COLUMNS = (
    Event.id, Event.title, Event.starts_at, Event.source, Event.created_at, Event.external_url,
    Event.source_event_id, Event.format, Event.venue_name, Event.address,
)


async def detect_duplicates(db: AsyncSession, now: datetime | None = None) -> int:
    """Flags new duplicates across every source (published, upcoming, not already linked)."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=1)
    candidates = [
        {**dict(row), "id": str(row["id"])}
        for row in (
            await db.execute(
                select(*_CANDIDATE_COLUMNS)
                .where(Event.status == "published", Event.duplicate_of.is_(None), Event.starts_at >= since)
                .order_by(Event.starts_at)
            )
        ).mappings()
    ]
    dismissals = (
        await db.execute(select(EventDuplicateDismissal.event_id, EventDuplicateDismissal.other_event_id))
    ).all()
    dismissed = {pair_key(str(a), str(b)) for a, b in dismissals}
    flagged = 0
    for match in find_duplicates(candidates, dismissed):
        try:
            result = await _savepoint(
                db,
                update(Event)
                .where(Event.id == UUID(match["id"]), Event.duplicate_of.is_(None))
                .values(duplicate_of=UUID(match["duplicate_of"]), needs_review=not match["exact"])
                .returning(Event.id),
            )
            flagged += len(result.all())
        except Exception:  # noqa: BLE001 - as in TS, a failed update just isn't counted
            pass
    await settle_certain_duplicates(db, since)
    return flagged


async def settle_certain_duplicates(db: AsyncSession, since: datetime) -> None:
    """Takes certain duplicates (same title, start and place) out of the review queue."""
    columns = (Event.id, Event.title, Event.starts_at, Event.format, Event.venue_name, Event.address, Event.duplicate_of)
    try:
        queued = [
            dict(row)
            for row in (
                await db.execute(
                    select(*columns)
                    .where(Event.duplicate_of.is_not(None), Event.needs_review.is_(True), Event.starts_at >= since)
                    .order_by(Event.id)
                )
            ).mappings()
        ]
        if not queued:
            return
        ids = list(dict.fromkeys(r["duplicate_of"] for r in queued))
        originals: dict[Any, dict[str, Any]] = {}
        for chunk in _chunks(ids):
            for row in (await db.execute(select(*columns).where(Event.id.in_(chunk)))).mappings():
                originals[row["id"]] = dict(row)
        settled = [r["id"] for r in queued if r["duplicate_of"] in originals and certain_duplicate(originals[r["duplicate_of"]], r)]
        for chunk in _chunks(settled):
            await _savepoint(
                db, update(Event).where(Event.id.in_(chunk), Event.duplicate_of.is_not(None)).values(needs_review=False)
            )
    except Exception:  # noqa: BLE001 - errors are swallowed, as in TS
        logger.warning("Couldn't settle certain duplicates", exc_info=True)


async def detect_duplicates_now(now: datetime | None = None) -> int:
    """`detect_duplicates` in its own trusted session (after a single source run)."""
    async with engine_session() as db:
        flagged = await detect_duplicates(db, now)
        await db.commit()
        return flagged


# ─── Refresh all ──────────────────────────────────────────────────────────────

REFRESH_LOCK = "orbit:refresh-sources"


async def refresh_all_sources(trigger: str = "cron") -> dict[str, Any]:
    """The nightly job: every enabled source in turn (one failure never stops the rest), then
    expiry and duplicate detection. Only one refresh runs at a time (a Postgres advisory lock)."""
    started_at = datetime.now(timezone.utc)
    errors: list[str] = []
    sources: list[dict[str, Any]] = []
    expired = 0
    duplicates_flagged = 0
    run = RunState(deadline=int(time.time() * 1000) + RUN_ENRICH_DEADLINE_MS)
    autopost: dict[str, Any] = {}

    # One connection holds the session-level lock for the whole refresh.
    async with engine.connect() as lock:
        locked = (await lock.execute(text("select pg_try_advisory_lock(hashtext(:k))"), {"k": REFRESH_LOCK})).scalar()
        await lock.commit()
        if not locked:
            return {
                "ok": False, "started_at": started_at.isoformat(), "finished_at": datetime.now(timezone.utc).isoformat(),
                "sources": [], "expired": 0, "duplicates_flagged": 0, "errors": ["A refresh is already running."],
            }
        try:
            try:
                async with engine_session() as db:
                    source_ids = (
                        await db.execute(select(EventSource.id).where(EventSource.enabled.is_(True)).order_by(EventSource.created_at))
                    ).scalars().all()
            except Exception as exc:  # noqa: BLE001
                source_ids = []
                errors.append(f"Couldn't list sources: {db_message(exc)}")
            for source_id in source_ids:
                try:
                    sources.append(await run_source(source_id, trigger, run=run))
                except Exception as exc:  # noqa: BLE001 - one source never stops the rest
                    errors.append(f"Source {source_id}: {error_message(exc)}")

            async with engine_session() as db:
                try:
                    expired = await expire_events(db)
                    await db.commit()
                except Exception as exc:  # noqa: BLE001
                    await db.rollback()
                    errors.append(f"Expiry: {db_message(exc)}")
                try:
                    duplicates_flagged = await detect_duplicates(db)
                    await db.commit()
                except Exception as exc:  # noqa: BLE001
                    await db.rollback()
                    errors.append(f"Duplicate check: {db_message(exc)}")

            # Its own transaction, and it never raises: the import has already succeeded or not.
            autopost = await run_autopost()
        finally:
            await lock.execute(text("select pg_advisory_unlock(hashtext(:k))"), {"k": REFRESH_LOCK})
            await lock.commit()

    summary = {
        "ok": not errors and all(s["status"] == "ok" for s in sources),
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "sources": sources,
        "expired": expired,
        "duplicates_flagged": duplicates_flagged,
        "errors": errors,
        "autopost": autopost,
    }
    logger.info(
        "Source refresh (%s) finished: ok=%s sources=%d expired=%d duplicates=%d errors=%s",
        trigger, summary["ok"], len(sources), expired, duplicates_flagged, errors,
    )
    return summary


_background: set[asyncio.Task] = set()


def start_refresh(trigger: str) -> bool:
    """Starts `refresh_all_sources` in the background (outside the HTTP request). False when one
    is already running in this process; across processes the advisory lock keeps it to one."""
    if any(not task.done() for task in _background):
        return False
    task = asyncio.create_task(refresh_all_sources(trigger))
    _background.add(task)
    task.add_done_callback(_finished)
    return True


def _finished(task: asyncio.Task) -> None:
    _background.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.error("Source refresh crashed", exc_info=task.exception())
