"""Typed wrappers for every Postgres function Orbit relies on.

These are the business rules — capacity, waitlists, approvals, check-in, role
changes, audit. They live in SQL (ported verbatim from the Supabase migrations,
see scripts/extract_sql.py) and this module is the only place Python calls
them, so nothing about payments or ticketing is re-implemented twice.

Every wrapper is a plain function: pass the request's AsyncSession.
"""

from typing import Any, Optional
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession


# ─── helpers ────────────────────────────────────────────────────────────────

async def _scalar(db: AsyncSession, sql: str, params: dict[str, Any]) -> Any:
    return (await db.execute(text(sql), params)).scalar()


async def _rows(db: AsyncSession, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(r) for r in (await db.execute(text(sql), params)).mappings().all()]


async def _row(db: AsyncSession, sql: str, params: dict[str, Any]) -> Optional[dict[str, Any]]:
    r = (await db.execute(text(sql), params)).mappings().first()
    return dict(r) if r else None


def raised_message(exc: DBAPIError) -> Optional[str]:
    """The sentence a function refused with (`raise exception`, SQLSTATE P0001), or None when
    the error is anything else (a real failure)."""
    raw = getattr(exc, "orig", None)
    if getattr(raw, "sqlstate", None) != "P0001":
        return None
    return (str(raw).strip().splitlines() or [""])[0].strip() or None


# ─── identity / authorisation helpers (also used by RLS-era guards) ─────────

async def auth_uid(db: AsyncSession) -> Optional[UUID]:
    return await _scalar(db, "select auth.uid()", {})


async def is_admin(db: AsyncSession) -> bool:
    return bool(await _scalar(db, "select public.is_admin()", {}))


async def is_super_admin(db: AsyncSession) -> bool:
    return bool(await _scalar(db, "select public.is_super_admin()", {}))


async def super_admin_email(db: AsyncSession) -> Optional[str]:
    return await _scalar(db, "select public.super_admin_email()", {})


async def is_org_member(db: AsyncSession, organiser_id: UUID) -> bool:
    return bool(await _scalar(db, "select public.is_org_member(:org)", {"org": organiser_id}))


async def is_org_owner(db: AsyncSession, organiser_id: UUID) -> bool:
    return bool(await _scalar(db, "select public.is_org_owner(:org)", {"org": organiser_id}))


async def can_manage_event(db: AsyncSession, event_id: UUID) -> bool:
    return bool(await _scalar(db, "select public.can_manage_event(:ev)", {"ev": event_id}))


async def set_user_role(db: AsyncSession, target_email: str, new_role: str) -> Optional[UUID]:
    """Promote/demote an admin by email. Super-admin only (checked by the route)."""
    return await _scalar(
        db,
        "select public.set_user_role(:email, :role)",
        {"email": target_email, "role": new_role},
    )


# ─── events ─────────────────────────────────────────────────────────────────

async def event_counts(db: AsyncSession, event_ids: list[UUID]) -> list[dict[str, Any]]:
    """going / waitlisted / pending per event, in one round trip."""
    return await _rows(
        db, "select * from public.event_counts(:ids)", {"ids": event_ids}
    )


async def event_search_text(
    db: AsyncSession, title: str, summary: str | None, description: str | None, tags: list[str]
) -> str:
    return await _scalar(
        db,
        "select public.event_search_text(:t, :s, :d, :tags)",
        {"t": title, "s": summary, "d": description, "tags": tags},
    )


# ─── tickets / registration ─────────────────────────────────────────────────

async def taken_spots(
    db: AsyncSession,
    event_id: UUID,
    ticket_type_id: UUID | None = None,
    exclude_user: UUID | None = None,
) -> int:
    return int(
        await _scalar(
            db,
            "select public.taken_spots(:e, :tt, :u)",
            {"e": event_id, "tt": ticket_type_id, "u": exclude_user},
        )
        or 0
    )


async def has_room(
    db: AsyncSession,
    event_id: UUID,
    ticket_type_id: UUID | None = None,
    exclude_user: UUID | None = None,
) -> bool:
    return bool(
        await _scalar(
            db,
            "select public.has_room(:e, :tt, :u)",
            {"e": event_id, "tt": ticket_type_id, "u": exclude_user},
        )
    )


async def ticket_availability(db: AsyncSession, event_id: UUID) -> list[dict[str, Any]]:
    return await _rows(
        db, "select * from public.ticket_availability(:e)", {"e": event_id}
    )


async def register_for_event(
    db: AsyncSession,
    event_id: UUID,
    ticket_type_id: UUID | None = None,
    answers: dict[str, Any] | None = None,
) -> Optional[dict[str, Any]]:
    """Applies every rule (deadline, capacity, approval, waitlist, payment) in one transaction."""
    return await _row(
        db,
        "select * from public.register_for_event(:e, :tt, cast(:a as jsonb))",
        {"e": event_id, "tt": ticket_type_id, "a": _json(answers or {})},
    )


async def cancel_registration(db: AsyncSession, event_id: UUID) -> Optional[dict[str, Any]]:
    return await _row(
        db, "select * from public.cancel_registration(:e)", {"e": event_id}
    )


async def decide_registration(
    db: AsyncSession, registration_id: UUID, decision: str
) -> Optional[dict[str, Any]]:
    """decision: 'approve' | 'decline'."""
    return await _row(
        db,
        "select * from public.decide_registration(:r, :d)",
        {"r": registration_id, "d": decision},
    )


async def promote_waitlist(db: AsyncSession, event_id: UUID) -> list[dict[str, Any]]:
    return await _rows(
        db, "select * from public.promote_waitlist(:e)", {"e": event_id}
    )


async def check_in(db: AsyncSession, event_id: UUID, code: str) -> Optional[dict[str, Any]]:
    """Door scan by QR/ticket code. Returns {ok, ...} or a reason."""
    return await _row(
        db, "select public.check_in(:e, :c) as result", {"e": event_id, "c": code}
    )


async def undo_check_in(db: AsyncSession, registration_id: UUID) -> None:
    await db.execute(
        text("select public.undo_check_in(:r)"), {"r": registration_id}
    )


async def payment_hold_ok(db: AsyncSession, registration_id: UUID) -> bool:
    return bool(
        await _scalar(
            db, "select public.payment_hold_ok(:r)", {"r": registration_id}
        )
    )


async def mark_registration_paid(
    db: AsyncSession,
    registration_id: UUID,
    checkout_session_id: str,
    payment_intent_id: str | None = None,
) -> Optional[dict[str, Any]]:
    """Webhook-only. Idempotent inside the function."""
    return await _row(
        db,
        "select * from public.mark_registration_paid(:r, :s, :p)",
        {"r": registration_id, "s": checkout_session_id, "p": payment_intent_id},
    )


# ─── organisers ─────────────────────────────────────────────────────────────

async def organiser_slug_available(db: AsyncSession, slug: str) -> bool:
    return bool(
        await _scalar(
            db, "select public.organiser_slug_available(:s)", {"s": slug}
        )
    )


async def organiser_follower_count(db: AsyncSession, organiser_id: UUID) -> int:
    return int(
        await _scalar(
            db, "select public.organiser_follower_count(:o)", {"o": organiser_id}
        )
        or 0
    )


async def organiser_members_list(db: AsyncSession, organiser_id: UUID) -> list[dict[str, Any]]:
    return await _rows(
        db,
        "select * from public.organiser_members_list(:o)",
        {"o": organiser_id},
    )


async def add_organiser_member(
    db: AsyncSession, organiser_id: UUID, member_email: str, role: str = "editor"
) -> Optional[UUID]:
    return await _scalar(
        db,
        "select public.add_organiser_member(:o, :e, :r)",
        {"o": organiser_id, "e": member_email, "r": role},
    )


async def remove_organiser_member(
    db: AsyncSession, organiser_id: UUID, user_id: UUID
) -> None:
    await db.execute(
        text("select public.remove_organiser_member(:o, :u)"),
        {"o": organiser_id, "u": user_id},
    )


async def create_organiser_invite(db: AsyncSession, organiser_id: UUID) -> Optional[str]:
    return await _scalar(
        db, "select public.create_organiser_invite(:o)", {"o": organiser_id}
    )


async def peek_organiser_invite(db: AsyncSession, code: str) -> Optional[dict[str, Any]]:
    return await _row(
        db, "select * from public.peek_organiser_invite(:c)", {"c": code}
    )


async def accept_organiser_invite(db: AsyncSession, code: str) -> Optional[dict[str, Any]]:
    return await _row(
        db, "select public.accept_organiser_invite(:c) as result", {"c": code}
    )


# ─── discovery ──────────────────────────────────────────────────────────────

async def discovery_counts(
    db: AsyncSession, interests: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    return await _rows(
        db,
        "select * from public.discovery_counts(cast(:i as jsonb))",
        {"i": _json(interests or {})},
    )


# ─── admin / notifications ──────────────────────────────────────────────────

async def log_admin_action(
    db: AsyncSession,
    action: str,
    target_type: str,
    target_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> Optional[UUID]:
    return await _scalar(
        db,
        "select public.log_admin_action(:a, :tt, :ti, cast(:d as jsonb))",
        {"a": action, "tt": target_type, "ti": target_id, "d": _json(details or {})},
    )


async def admin_stats(db: AsyncSession) -> dict[str, Any]:
    return await _scalar(db, "select public.admin_stats()", {}) or {}


async def admin_signups_daily(db: AsyncSession, days: int = 30) -> list[dict[str, Any]]:
    return await _rows(
        db, "select * from public.admin_signups_daily(:d)", {"d": days}
    )


async def save_push_subscription(
    db: AsyncSession, endpoint: str, p256dh: str, auth: str, user_agent: str | None = None
) -> Optional[UUID]:
    return await _scalar(
        db,
        "select public.save_push_subscription(:e, :p, :a, :u)",
        {"e": endpoint, "p": p256dh, "a": auth, "u": user_agent},
    )


def _json(value: Any) -> str:
    import json

    return json.dumps(value)
