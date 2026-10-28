"""Database work for organisers: profile, public page, team, invites, follows.

Every rule that already lives in SQL (slug uniqueness, team roles, invite
redemption) is delegated to app/sql/functions.py. The queries here are reads
and the few writes the functions do not own.
"""

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import offset
from app.sql import functions as fn

ORGANISER_COLUMNS = (
    "id, slug, name, kind, logo_url, bio, website, contact_email, university, "
    "status, is_verified, review_note, reviewed_by, reviewed_at, stripe_account_id, "
    "stripe_charges_enabled, created_by, created_at, updated_at"
)

EVENT_COLUMNS = (
    "id, organiser_id, title, summary, description, cover_url, starts_at, ends_at, "
    "timezone, format, venue_name, address, city, country, latitude, longitude, "
    "category_id, tags, university, registration_mode, external_url, external_label, "
    "requires_approval, capacity, waitlist_enabled, registration_deadline, price_min, "
    "price_max, currency, status, cancel_reason, is_featured, needs_review, published_at, "
    "source, source_url, source_event_id, last_verified_at, series_key, series_label, "
    "created_by, created_at, updated_at"
)

INVITE_COLUMNS = "id, organiser_id, code, role, created_by, expires_at, max_uses, uses, revoked, created_at"

ORGANISER_COLUMNS_ALIASED = ", ".join(
    f"o.{c.strip()}" for c in ORGANISER_COLUMNS.split(",")
)


async def _one(db: AsyncSession, sql: str, params: dict[str, Any]) -> Optional[dict]:
    row = (await db.execute(text(sql), params)).mappings().first()
    return dict(row) if row else None


async def _all(db: AsyncSession, sql: str, params: dict[str, Any]) -> list[dict]:
    return [dict(r) for r in (await db.execute(text(sql), params)).mappings().all()]


async def _val(db: AsyncSession, sql: str, params: dict[str, Any]) -> Any:
    return (await db.execute(text(sql), params)).scalar()


async def with_counts(db: AsyncSession, events: list[dict]) -> list[dict]:
    """Attach going / waitlisted / pending from event_counts in one round trip."""
    if not events:
        return events
    rows = await fn.event_counts(db, [e["id"] for e in events])
    by_id = {str(r["event_id"]): r for r in rows}
    for event in events:
        c = by_id.get(str(event["id"]))
        event["counts"] = {
            "going": int(c["going"]) if c else 0,
            "waitlisted": int(c["waitlisted"]) if c else 0,
            "pending": int(c["pending"]) if c else 0,
        }
    return events


async def org_role(
    db: AsyncSession, organiser_id: UUID | str, user: dict, *, owner: bool = False
) -> str:
    """The caller's role on the organiser, or 403. Admins act as admins."""
    if user["role"] in ("admin", "super_admin"):
        return "admin"
    role = await _val(
        db,
        "select role from organiser_members where organiser_id = :o and user_id = :u",
        {"o": organiser_id, "u": user["id"]},
    )
    if not role:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not a member of this organiser")
    if owner and role != "owner":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the owner can do that")
    return role


async def _get(db: AsyncSession, organiser_id: UUID | str) -> dict:
    org = await _one(
        db,
        f"select {ORGANISER_COLUMNS} from organisers where id = :o",
        {"o": organiser_id},
    )
    if not org:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Organiser not found")
    return org


async def page_for_member(
    db: AsyncSession, user_id: UUID | str, page: int, page_size: int
) -> dict:
    """Organisers the caller belongs to, with their role and the organiser's status."""
    total = int(
        await _val(
            db,
            "select count(*) from organiser_members where user_id = :u",
            {"u": user_id},
        )
        or 0
    )
    items = await _all(
        db,
        f"""
        select {ORGANISER_COLUMNS_ALIASED}, m.role as member_role
          from organisers o
          join organiser_members m on m.organiser_id = o.id
         where m.user_id = :u
         order by o.name
         limit :lim offset :off
        """,
        {"u": user_id, "lim": page_size, "off": offset(page, page_size)},
    )
    return {"items": items, "total": total, "page": page, "page_size": page_size}


async def slug_available(db: AsyncSession, slug: str) -> bool:
    return await fn.organiser_slug_available(db, slug.strip().lower())


async def create_organiser(db: AsyncSession, data: dict, user_id: UUID | str) -> dict:
    """Insert a pending organiser and make the caller its owner, in one transaction."""
    slug = str(data["slug"]).strip().lower()
    if not await fn.organiser_slug_available(db, slug):
        raise HTTPException(status.HTTP_409_CONFLICT, "That link is taken")

    try:
        org = await _one(
            db,
            f"""
            insert into organisers
              (name, slug, kind, bio, website, contact_email, university, logo_url, status, created_by)
            values
              (:name, :slug, :kind, :bio, :website, :contact_email, :university, :logo_url,
               'pending', :created_by)
            returning {ORGANISER_COLUMNS}
            """,
            {
                "name": data["name"].strip(),
                "slug": slug,
                "kind": data["kind"],
                "bio": data.get("bio"),
                "website": data.get("website"),
                "contact_email": str(data["contact_email"]),
                "university": data.get("university"),
                "logo_url": data.get("logo_url"),
                "created_by": user_id,
            },
        )
    except IntegrityError as exc:  # lost the race between the check and the insert
        raise HTTPException(status.HTTP_409_CONFLICT, "That link is taken") from exc

    await db.execute(
        text(
            """
            insert into organiser_members (organiser_id, user_id, role)
            values (:o, :u, 'owner')
            on conflict (organiser_id, user_id) do nothing
            """
        ),
        {"o": org["id"], "u": user_id},
    )
    return {**org, "member_role": "owner"}


async def update_organiser(
    db: AsyncSession, organiser_id: UUID | str, data: dict
) -> dict:
    if not data:
        return await _get(db, organiser_id)
    sets = ", ".join(f"{key} = :{key}" for key in data)
    org = await _one(
        db,
        f"update organisers set {sets}, updated_at = now() where id = :id"
        f" returning {ORGANISER_COLUMNS}",
        {**data, "id": organiser_id},
    )
    if not org:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Organiser not found")
    return org


async def organiser_page(
    db: AsyncSession, slug: str, user: Optional[dict]
) -> Optional[dict]:
    """The public organiser page: profile, follower count, follow state, upcoming events."""
    org = await _one(
        db,
        f"select {ORGANISER_COLUMNS} from organisers where slug = lower(:slug)",
        {"slug": slug.strip()},
    )
    if not org:
        return None

    is_member = False
    if user:
        if user["role"] in ("admin", "super_admin"):
            is_member = True
        else:
            is_member = bool(
                await _val(
                    db,
                    "select exists(select 1 from organiser_members"
                    " where organiser_id = :o and user_id = :u)",
                    {"o": org["id"], "u": user["id"]},
                )
            )
    if org["status"] != "approved" and not is_member:
        return None

    following = False
    if user:
        following = bool(
            await _val(
                db,
                "select exists(select 1 from follows where organiser_id = :o and user_id = :u)",
                {"o": org["id"], "u": user["id"]},
            )
        )

    events = await _all(
        db,
        f"""
        select {EVENT_COLUMNS} from events
         where organiser_id = :o and status = 'published' and duplicate_of is null
           and coalesce(ends_at, starts_at) >= now()
         order by starts_at
         limit 100
        """,
        {"o": org["id"]},
    )
    return {
        "organiser": org,
        "follower_count": await fn.organiser_follower_count(db, org["id"]),
        "following": following,
        "events": await with_counts(db, events),
    }


# Public organiser page lists: (where, order) per `when`. Drafts and removed
# events are never public.
PUBLIC_EVENT_LISTS = {
    "upcoming": (
        "status = 'published' and coalesce(ends_at, starts_at) >= now()",
        "starts_at",
    ),
    "past": (
        "status in ('published', 'cancelled', 'expired')"
        " and (status = 'expired' or coalesce(ends_at, starts_at) < now())",
        "starts_at desc",
    ),
    "all": ("status in ('published', 'cancelled', 'expired')", "starts_at"),
}


async def public_events_page(
    db: AsyncSession,
    organiser_id: UUID | str,
    page: int,
    page_size: int,
    when: str = "upcoming",
) -> dict:
    """An organiser's public events, paged, with guest counts."""
    condition, order = PUBLIC_EVENT_LISTS[when]
    where = f"organiser_id = :o and duplicate_of is null and {condition}"
    total = int(
        await _val(
            db, f"select count(*) from events where {where}", {"o": organiser_id}
        )
        or 0
    )
    items = await _all(
        db,
        f"select {EVENT_COLUMNS} from events where {where}"
        f" order by {order}, id limit :lim offset :off",
        {"o": organiser_id, "lim": page_size, "off": offset(page, page_size)},
    )
    return {
        "items": await with_counts(db, items),
        "total": total,
        "page": page,
        "page_size": page_size,
    }


async def set_follow(
    db: AsyncSession, organiser_id: UUID | str, user_id: UUID | str, following: bool
) -> bool:
    await _get(db, organiser_id)
    if following:
        await db.execute(
            text(
                "insert into follows (organiser_id, user_id) values (:o, :u)"
                " on conflict (organiser_id, user_id) do nothing"
            ),
            {"o": organiser_id, "u": user_id},
        )
    else:
        await db.execute(
            text("delete from follows where organiser_id = :o and user_id = :u"),
            {"o": organiser_id, "u": user_id},
        )
    return following


# How a team or invite rule the SQL refused is answered; the body is the function's own
# sentence, which the frontend shows as it did with the Supabase RPC error.
REFUSAL_STATUS: tuple[tuple[str, int], ...] = (
    ("Every organiser needs at least one owner", status.HTTP_409_CONFLICT),
    ("That person is not on this team", status.HTTP_404_NOT_FOUND),
    ("This invite link is not valid", status.HTTP_404_NOT_FOUND),
    ("This invite link has been turned off", status.HTTP_409_CONFLICT),
    ("This invite link has expired", status.HTTP_409_CONFLICT),
    ("This invite link has reached its limit", status.HTTP_409_CONFLICT),
    ("This organisation is not accepting team members", status.HTTP_409_CONFLICT),
    ("Only owners can", status.HTTP_403_FORBIDDEN),
    ("Sign in", status.HTTP_401_UNAUTHORIZED),
)


def _refusal(exc: DBAPIError) -> HTTPException:
    """The HTTP answer for a refusal raised by a team/invite function; other errors re-raise."""
    message = fn.raised_message(exc)
    if message is None:
        raise exc
    code = next(
        (code for prefix, code in REFUSAL_STATUS if message.startswith(prefix)),
        status.HTTP_400_BAD_REQUEST,
    )
    return HTTPException(code, message)


async def team(db: AsyncSession, organiser_id: UUID | str) -> list[dict]:
    return await fn.organiser_members_list(db, organiser_id)


async def add_member(
    db: AsyncSession, organiser_id: UUID | str, email: str, role: str
) -> dict:
    # The team panel only ever adds people: someone already on the team keeps their role.
    # (The SQL upsert would quietly change it, even demoting the only owner.)
    existing = await _val(
        db,
        """
        select m.role
          from organiser_members m
          join profiles p on p.id = m.user_id
         where m.organiser_id = :o and lower(p.email) = lower(:e)
         limit 1
        """,
        {"o": organiser_id, "e": str(email).strip()},
    )
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "already_on_team")
    try:
        # Refuses an unknown email with the sentence the team panel showed.
        user_id = await fn.add_organiser_member(db, organiser_id, str(email).strip(), role)
    except DBAPIError as exc:
        raise _refusal(exc) from exc
    row = await _one(
        db,
        """
        select m.user_id, p.full_name, p.email, p.avatar_url, m.role, m.created_at
          from organiser_members m
          left join profiles p on p.id = m.user_id
         where m.organiser_id = :o and m.user_id = :u
        """,
        {"o": organiser_id, "u": user_id},
    )
    return row or {"ok": True, "user_id": str(user_id)}


async def remove_member(
    db: AsyncSession, organiser_id: UUID | str, user_id: UUID | str
) -> None:
    try:
        await fn.remove_organiser_member(db, organiser_id, user_id)
    except DBAPIError as exc:
        raise _refusal(exc) from exc


async def list_invites(
    db: AsyncSession, organiser_id: UUID | str, page: int, page_size: int
) -> dict:
    total = int(
        await _val(
            db,
            "select count(*) from organiser_invites where organiser_id = :o",
            {"o": organiser_id},
        )
        or 0
    )
    items = await _all(
        db,
        f"select {INVITE_COLUMNS} from organiser_invites where organiser_id = :o"
        " order by created_at desc limit :lim offset :off",
        {"o": organiser_id, "lim": page_size, "off": offset(page, page_size)},
    )
    return {"items": items, "total": total, "page": page, "page_size": page_size}


async def create_invite(db: AsyncSession, organiser_id: UUID | str) -> dict:
    org = await _get(db, organiser_id)
    if org["status"] != "approved":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Team invites unlock once the organisation is approved",
        )
    code = await fn.create_organiser_invite(db, organiser_id)
    if not code:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Could not create an invite link"
        )
    return {"code": code}


async def revoke_invite(db: AsyncSession, invite_id: UUID | str, user: dict) -> dict:
    invite = await _one(
        db,
        f"select {INVITE_COLUMNS} from organiser_invites where id = :id",
        {"id": invite_id},
    )
    if not invite:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invite not found")
    await org_role(db, invite["organiser_id"], user, owner=True)
    await db.execute(
        text("update organiser_invites set revoked = true where id = :id"),
        {"id": invite_id},
    )
    return {**invite, "revoked": True}


def invite_problem(invite: dict, now: datetime | None = None) -> Optional[str]:
    """Why a code cannot be used right now, in the order the join page explains it."""
    now = now or datetime.now(timezone.utc)
    if invite["revoked"]:
        return "revoked"
    if invite["expires_at"] <= now:
        return "expired"
    if invite["uses"] >= invite["max_uses"]:
        return "used_up"
    if invite["org_status"] != "approved":
        return "org_inactive"
    return None


async def peek_invite(db: AsyncSession, code: str) -> dict:
    """What the join page renders. 404 only for an unknown code; a dead code says why."""
    invite = await fn.peek_organiser_invite(db, code)
    if not invite:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This invite link is not valid")
    reason = invite_problem(invite)
    return {
        "organiser": {
            "id": invite["organiser_id"],
            "name": invite["organiser_name"],
            "slug": invite["organiser_slug"],
            "logo_url": invite["logo_url"],
            "status": invite["org_status"],
        },
        "role": invite["role"],
        "expires_at": invite["expires_at"],
        "revoked": invite["revoked"],
        "max_uses": invite["max_uses"],
        "uses": invite["uses"],
        "uses_left": max(0, invite["max_uses"] - invite["uses"]),
        "already_member": invite["already_member"],
        "valid": reason is None,
        "reason": reason,
    }


async def accept_invite(db: AsyncSession, code: str) -> dict:
    try:
        result = await fn.accept_organiser_invite(db, code)
    except DBAPIError as exc:
        raise _refusal(exc) from exc
    payload = (result or {}).get("result")
    if not payload:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This invite link is not valid")
    return payload
