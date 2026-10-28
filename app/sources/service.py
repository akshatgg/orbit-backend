"""Source sync engine: fetch → plan → apply, plus expiry and duplicate detection.

The rules mirror Orbit's TypeScript engine (src/lib/sources/engine.ts and
sync-plan.ts). Orbit never auto-cancels: an event missing or cancelled upstream is
only flagged for review, and only on the run where it first goes missing.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence
from urllib.parse import urlparse
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import ConnectorError, FetchedFeed
from app.connectors.registry import CONNECTORS
from app.core.config import settings
from app.events.models import Event
from app.sources.models import EventDuplicateDismissal, EventSource, SourceRun

SYNCED_FIELDS: tuple[str, ...] = (
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
    "registration_mode",
    "external_url",
    "source_url",
    "series_key",
    "series_label",
)

SKIPPED_FIELDS: dict[str, tuple[str, ...]] = {
    "luma": ("description", "city"),
    "luma-discover": ("description", "city"),
    "meetup": ("venue_name", "address", "city", "latitude", "longitude"),
    "meetup-search": ("venue_name", "address", "city", "latitude", "longitude"),
    "eventbrite": ("description",),
}

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

_COUNTRY_NAMES = {
    "united kingdom": "GB",
    "great britain": "GB",
    "uk": "GB",
    "england": "GB",
    "scotland": "GB",
    "wales": "GB",
    "ireland": "IE",
    "united states": "US",
    "usa": "US",
    "united states of america": "US",
    "india": "IN",
    "france": "FR",
    "germany": "DE",
    "spain": "ES",
    "italy": "IT",
    "netherlands": "NL",
    "canada": "CA",
    "australia": "AU",
    "singapore": "SG",
    "united arab emirates": "AE",
}


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


def _utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or settings.DEFAULT_TIMEZONE)
    except Exception:
        return ZoneInfo(settings.DEFAULT_TIMEZONE)


def _local_day(value: datetime, tz_name: str | None) -> str:
    return _utc(value).astimezone(_zone(tz_name)).date().isoformat()


def normalise_country(value: Any) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if len(raw) == 2 and raw.isalpha():
        return raw.upper()
    return _COUNTRY_NAMES.get(raw.lower())


def provenance_url(event: dict[str, Any], feed_url: str | None) -> str | None:
    """The event's own page, else the feed's origin (never a URL that may hold a token)."""
    url = event.get("url")
    if isinstance(url, str) and url.lower().startswith(("http://", "https://")):
        return url
    if not feed_url:
        return None
    try:
        parsed = urlparse(feed_url)
        if parsed.scheme and parsed.hostname:
            return f"{parsed.scheme}://{parsed.hostname}"
    except ValueError:
        return None
    return None


def synced_fields(event: dict[str, Any], feed_url: str | None = None) -> dict[str, Any]:
    """The fields a source owns on its events."""
    country = normalise_country(event.get("country"))
    latitude = event.get("latitude")
    longitude = event.get("longitude")
    data: dict[str, Any] = {
        "title": str(event.get("title") or "").strip()[:140],
        "summary": (str(event["summary"])[:200] if event.get("summary") else None),
        "description": event.get("description"),
        "starts_at": _utc(event.get("starts_at")),
        "ends_at": _utc(event.get("ends_at")),
        "timezone": event.get("timezone") or settings.DEFAULT_TIMEZONE,
        "format": event.get("format") if event.get("format") in ("in_person", "online", "hybrid") else "in_person",
        "venue_name": event.get("venue_name"),
        "address": event.get("address"),
        "city": event.get("city"),
        "latitude": float(latitude) if latitude is not None else None,
        "longitude": float(longitude) if longitude is not None else None,
        "registration_mode": "external" if event.get("url") else "none",
        "external_url": event.get("url"),
        "source_url": provenance_url(event, feed_url),
        "series_key": event.get("series_key"),
        "series_label": event.get("series_label"),
    }
    if country:
        data["country"] = country
    return data


def _same_coordinate(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    try:
        return abs(float(a) - float(b)) < 1e-6
    except (TypeError, ValueError):
        return a == b


def changed_fields(
    existing: dict[str, Any], incoming: dict[str, Any], skip: Iterable[str] = ()
) -> dict[str, Any]:
    """Only the fields that actually changed."""
    next_fields = {key: value for key, value in incoming.items() if key not in set(skip)}
    if existing.get("registration_mode") == "tickets":
        next_fields.pop("registration_mode", None)
        next_fields.pop("external_url", None)
    patch: dict[str, Any] = {}
    for key, value in next_fields.items():
        current = existing.get(key)
        if key in ("starts_at", "ends_at"):
            same = _utc(current) == _utc(value)
        elif key in ("latitude", "longitude"):
            same = _same_coordinate(current, value)
        else:
            same = current == value
        if not same:
            patch[key] = value
    return patch


def build_insert_row(
    event: dict[str, Any], source: dict[str, Any], now: datetime, category_id: UUID | None | object = ...
) -> dict[str, Any]:
    """Row for an event Orbit hasn't seen before; prices are set only on insert."""
    key = source_key(source["id"])
    row = synced_fields(event, source.get("url"))
    row.update(
        {
            "source": key,
            "source_event_id": event["source_event_id"],
            "external_label": EXTERNAL_LABELS.get(source.get("kind") or "ical", "View details"),
            "organiser_id": source.get("default_organiser_id"),
            "category_id": source.get("default_category_id") if category_id is ... else category_id,
            "tags": list(source.get("default_tags") or []),
            "cover_url": event.get("cover_url"),
            "status": "published",
            "needs_review": False,
            "last_verified_at": now,
            "created_at": now,
            "updated_at": now,
        }
    )
    # Executemany inserts need every row to carry the same keys.
    row.setdefault("country", None)
    row.setdefault("price_min", None)
    row.setdefault("price_max", None)
    row.setdefault("currency", settings.CURRENCY)
    if event.get("price_min") is not None or event.get("price_max") is not None:
        row["price_min"] = event.get("price_min")
        row["price_max"] = event.get("price_max")
        row["currency"] = event.get("currency") or settings.CURRENCY
    return row


def _rekey_map(
    existing: Sequence[dict[str, Any]], incoming: Sequence[dict[str, Any]]
) -> dict[tuple[str, datetime], dict[str, Any]]:
    incoming_ids = {event["source_event_id"] for event in incoming}
    by_series: dict[tuple[str, datetime], dict[str, Any]] = {}
    for row in existing:
        series = row.get("series_key")
        source_event_id = row.get("source_event_id")
        starts = _utc(row.get("starts_at"))
        if series and source_event_id and source_event_id not in incoming_ids and starts is not None:
            by_series[(series, starts)] = row
    return by_series


def plan_sync(
    existing: Sequence[dict[str, Any]],
    incoming: Sequence[dict[str, Any]],
    now: datetime,
    previous_run_started_at: datetime | None = None,
    coverage: str | dict[str, Any] = "complete",
    *,
    skip: Iterable[str] = (),
    feed_url: str | None = None,
) -> dict[str, list[Any]]:
    """Diff one source's feed against its stored events, mirroring Orbit's rules."""
    now = _utc(now) or datetime.now(timezone.utc)
    previous = _utc(previous_run_started_at)
    plan: dict[str, list[Any]] = {
        "inserts": [],
        "updates": [],
        "unchanged": [],
        "flags": [],
        "unflags": [],
    }
    by_id = {row["source_event_id"]: row for row in existing if row.get("source_event_id")}
    seen_ids: set[str] = set()
    series_map = _rekey_map(existing, incoming)

    def was_seen_last_run(row: dict[str, Any]) -> bool:
        if previous is None:
            return True
        verified = _utc(row.get("last_verified_at"))
        return verified is not None and verified >= previous

    def flag(row: dict[str, Any], reason: str) -> None:
        if (
            not row.get("needs_review")
            and row.get("status") in LIVE_STATUSES
            and was_seen_last_run(row)
        ):
            plan["flags"].append({"id": row["id"], "reason": reason})

    for event in incoming:
        source_event_id = event["source_event_id"]
        if source_event_id in seen_ids:
            continue
        seen_ids.add(source_event_id)
        current = by_id.get(source_event_id)
        rekeyed = False
        if current is None and event.get("series_key"):
            starts = _utc(synced_fields(event, feed_url).get("starts_at"))
            if starts is not None:
                current = series_map.get((event["series_key"], starts))
            if current is not None:
                rekeyed = True
                seen_ids.add(current["source_event_id"])
                series_map.pop((event["series_key"], starts), None)

        if event.get("cancelled"):
            if current is not None:
                flag(current, "cancelled")
            continue

        if current is None:
            plan["inserts"].append(event)
            continue

        if current.get("needs_review") and not current.get("duplicate_of"):
            plan["unflags"].append(current["id"])

        patch = changed_fields(current, synced_fields(event, feed_url), skip)
        if rekeyed:
            patch["source_event_id"] = source_event_id
        if patch:
            plan["updates"].append({"id": current["id"], "source_event_id": source_event_id, "patch": patch})
        else:
            plan["unchanged"].append(current["id"])

    # A flag on an event that has started is moot.
    unflagged = set(plan["unflags"])
    for row in existing:
        starts = _utc(row.get("starts_at"))
        if (
            row.get("needs_review")
            and not row.get("duplicate_of")
            and row["id"] not in unflagged
            and starts is not None
            and starts <= now
        ):
            plan["unflags"].append(row["id"])
            unflagged.add(row["id"])

    if coverage == "sample":
        return plan

    covered_until = (
        _utc(coverage["until"]) if isinstance(coverage, dict) and coverage.get("until") else None
    )
    for row in existing:
        source_event_id = row.get("source_event_id")
        if not source_event_id or source_event_id in seen_ids:
            continue
        starts = _utc(row.get("starts_at"))
        if starts is None or starts <= now:
            continue
        if covered_until is not None and starts >= covered_until:
            continue
        if _local_day(starts, row.get("timezone")) == _local_day(now, row.get("timezone")):
            if row.get("needs_review") and not row.get("duplicate_of") and row["id"] not in unflagged:
                plan["unflags"].append(row["id"])
                unflagged.add(row["id"])
            continue
        flag(row, "missing")

    return plan


def _chunks(items: Sequence[Any], size: int = 500) -> Iterable[Sequence[Any]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


EXISTING_COLUMNS = (
    Event.id,
    Event.source_event_id,
    Event.title,
    Event.summary,
    Event.description,
    Event.starts_at,
    Event.ends_at,
    Event.timezone,
    Event.format,
    Event.venue_name,
    Event.address,
    Event.city,
    Event.country,
    Event.latitude,
    Event.longitude,
    Event.registration_mode,
    Event.external_url,
    Event.source_url,
    Event.series_key,
    Event.series_label,
    Event.status,
    Event.needs_review,
    Event.duplicate_of,
    Event.last_verified_at,
)


async def get_source(db: AsyncSession, source_id: UUID) -> EventSource | None:
    return (
        await db.execute(select(EventSource).where(EventSource.id == source_id))
    ).scalar_one_or_none()


async def _previous_ok_run(db: AsyncSession, source_id: UUID) -> datetime | None:
    return (
        await db.execute(
            select(SourceRun.started_at)
            .where(SourceRun.source_id == source_id, SourceRun.status == "ok")
            .order_by(SourceRun.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def _error_text(errors: list[dict[str, Any]]) -> str:
    for error in errors:
        message = error.get("message")
        if message:
            return str(message)[:500]
    return "Unknown error"


async def run_source(
    db: AsyncSession, source_id: UUID, trigger: str = "manual"
) -> dict[str, Any]:
    """Fetch one source and apply the diff. Never raises: failures are recorded on the run."""
    started_at = datetime.now(timezone.utc)
    errors: list[dict[str, Any]] = []
    summary: dict[str, Any] = {
        "source_id": str(source_id),
        "name": "",
        "status": "error",
        "found": 0,
        "inserted": 0,
        "updated": 0,
        "unchanged": 0,
        "flagged_missing": 0,
        "enriched": 0,
        "errors": errors,
    }
    source: dict[str, Any] | None = None

    try:
        row = await get_source(db, source_id)
        if row is None:
            errors.append({"message": "Source not found"})
            return summary
        source = {
            "id": row.id,
            "name": row.name,
            "kind": row.kind,
            "url": row.url,
            "default_organiser_id": row.default_organiser_id,
            "default_category_id": row.default_category_id,
            "default_tags": list(row.default_tags or []),
            "permission_confirmed": row.permission_confirmed,
            "auto_category": row.auto_category,
        }
        summary["name"] = source["name"]

        if not source["permission_confirmed"]:
            raise ConnectorError("Permission to use this feed hasn't been confirmed.")
        connector = CONNECTORS.get(source["kind"])
        if connector is None:
            raise ConnectorError(f'No connector for "{source["kind"]}" sources.')

        previous_run_started_at = await _previous_ok_run(db, source_id)
        fetched: FetchedFeed = await connector.fetch(source)
        errors.extend(fetched.errors[:50])
        summary["found"] = len(fetched.events)

        key = source_key(source_id)
        existing = [
            dict(row)
            for row in (
                await db.execute(select(*EXISTING_COLUMNS).where(Event.source == key))
            ).mappings()
        ]
        coverage: str | dict[str, Any] = (
            "sample" if source["kind"] in ADD_ONLY_KINDS else fetched.coverage
        )
        plan = plan_sync(
            existing,
            fetched.events,
            started_at,
            previous_run_started_at,
            coverage,
            skip=SKIPPED_FIELDS.get(source["kind"], ()),
            feed_url=source["url"],
        )

        if plan["inserts"]:
            rows = [build_insert_row(event, source, started_at) for event in plan["inserts"]]
            await db.execute(insert(Event).values(rows))
            summary["inserted"] = len(rows)

        for change in plan["updates"]:
            patch = dict(change["patch"])
            patch["last_verified_at"] = started_at
            patch["updated_at"] = started_at
            result = await db.execute(
                update(Event)
                .where(Event.id == change["id"], Event.source == key)
                .values(**patch)
            )
            summary["updated"] += result.rowcount or 0

        for ids in _chunks(plan["unchanged"]):
            await db.execute(
                update(Event)
                .where(Event.id.in_(ids), Event.source == key)
                .values(last_verified_at=started_at)
            )
            summary["unchanged"] += len(ids)

        for ids in _chunks([flag["id"] for flag in plan["flags"]]):
            await db.execute(
                update(Event)
                .where(Event.id.in_(ids), Event.source == key)
                .values(needs_review=True, updated_at=started_at)
            )
            summary["flagged_missing"] += len(ids)

        for ids in _chunks(plan["unflags"]):
            await db.execute(
                update(Event)
                .where(
                    Event.id.in_(ids),
                    Event.source == key,
                    Event.duplicate_of.is_(None),
                )
                .values(needs_review=False, updated_at=started_at)
            )

        summary["status"] = "ok"
    except Exception as exc:  # noqa: BLE001 - the run records the failure
        errors.insert(0, {"message": str(exc) or exc.__class__.__name__})
        summary["status"] = "error"
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass

    if source is None:
        return summary

    finished_at = datetime.now(timezone.utc)
    try:
        await db.execute(
            insert(SourceRun).values(
                source_id=source_id,
                trigger=trigger if trigger in ("cron", "manual") else "manual",
                started_at=started_at,
                finished_at=finished_at,
                status=summary["status"],
                found=summary["found"],
                inserted=summary["inserted"],
                updated=summary["updated"],
                unchanged=summary["unchanged"],
                flagged_missing=summary["flagged_missing"],
                enriched=summary["enriched"],
                errors=errors[:50],
            )
        )
        await db.execute(
            update(EventSource)
            .where(EventSource.id == source_id)
            .values(
                last_run_at=finished_at,
                last_status=summary["status"],
                last_error=_error_text(errors) if summary["status"] == "error" else None,
                updated_at=finished_at,
            )
        )
    except Exception as exc:  # noqa: BLE001
        summary["errors"].append({"message": f"Couldn't record the run: {exc}"})
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
    return summary


async def expire_events(db: AsyncSession, now: datetime | None = None) -> int:
    """Published events that ended more than a day ago become expired."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=1)
    ended = await db.execute(
        update(Event)
        .where(Event.status == "published", Event.ends_at.is_not(None), Event.ends_at < cutoff)
        .values(status="expired", updated_at=now)
    )
    no_end = await db.execute(
        update(Event)
        .where(Event.status == "published", Event.ends_at.is_(None), Event.starts_at < cutoff)
        .values(status="expired", updated_at=now)
    )
    return (ended.rowcount or 0) + (no_end.rowcount or 0)


def _pair_key(a: Any, b: Any) -> tuple[str, str]:
    first, second = sorted((str(a), str(b)))
    return first, second


async def detect_duplicates(db: AsyncSession, now: datetime | None = None) -> int:
    """Flag published events sharing (lower(title), starts_at) and point them at the older row."""
    now = now or datetime.now(timezone.utc)
    rows = (
        await db.execute(
            select(Event.id, Event.title, Event.starts_at, Event.created_at)
            .where(Event.status == "published", Event.duplicate_of.is_(None))
        )
    ).mappings().all()
    dismissals = (
        await db.execute(
            select(
                EventDuplicateDismissal.event_id,
                EventDuplicateDismissal.other_event_id,
            )
        )
    ).mappings().all()
    dismissed = {_pair_key(row["event_id"], row["other_event_id"]) for row in dismissals}

    groups: dict[tuple[str, datetime], list[dict[str, Any]]] = {}
    for row in rows:
        starts = _utc(row["starts_at"])
        if starts is None:
            continue
        groups.setdefault((str(row["title"]).strip().lower(), starts), []).append(dict(row))

    flagged = 0
    for group in groups.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda item: (_utc(item["created_at"]) or now, str(item["id"])))
        keeper = group[0]
        for candidate in group[1:]:
            if _pair_key(keeper["id"], candidate["id"]) in dismissed:
                continue
            result = await db.execute(
                update(Event)
                .where(Event.id == candidate["id"], Event.duplicate_of.is_(None))
                .values(duplicate_of=keeper["id"], needs_review=True, updated_at=now)
            )
            flagged += result.rowcount or 0
    return flagged


async def refresh_all_sources(
    db: AsyncSession, trigger: str = "cron"
) -> dict[str, Any]:
    """Every enabled source in turn, then expiry and duplicate detection."""
    started_at = datetime.now(timezone.utc)
    errors: list[str] = []
    sources: list[dict[str, Any]] = []

    source_ids = (
        await db.execute(
            select(EventSource.id)
            .where(EventSource.enabled.is_(True))
            .order_by(EventSource.created_at)
        )
    ).scalars().all()
    for source_id in source_ids:
        try:
            sources.append(await run_source(db, source_id, trigger))
            await db.commit()
        except Exception as exc:  # noqa: BLE001 - one source never stops the rest
            await db.rollback()
            errors.append(f"Source {source_id}: {exc}")

    expired = 0
    try:
        expired = await expire_events(db)
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        await db.rollback()
        errors.append(f"Expiry: {exc}")

    duplicates_flagged = 0
    try:
        duplicates_flagged = await detect_duplicates(db)
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        await db.rollback()
        errors.append(f"Duplicate check: {exc}")

    return {
        "ok": not errors and all(source["status"] == "ok" for source in sources),
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "sources": sources,
        "expired": expired,
        "duplicates_flagged": duplicates_flagged,
        "errors": errors,
    }
