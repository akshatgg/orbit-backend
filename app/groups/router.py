"""Circles (friends coordinating) and Communities (interest-based).

One resource serves both: `kind` decides the rules. A Circle is private and
invitation-driven; a Community is listed in the directory and is either open
(join instantly) or approval-based. Communities are created by admins — the
PDF's "admin-created communities" MVP — while any signed-in user can create a
Circle.
"""

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import current_user, require_user
from app.groups.schemas import (
    AnnouncementCreate,
    GroupCreate,
    GroupUpdate,
    InterestBody,
    InviteCreate,
    InviteRespond,
    ShareEvent,
)
from app.models import Group, GroupAnnouncement, GroupEvent, GroupEventInterest, GroupInvite, GroupMember

router = APIRouter(prefix="/groups", tags=["groups"])

GROUP_CARD = """
select g.id, g.kind, g.name, g.slug, g.description, g.cover_url, g.university,
       g.visibility, g.category_id, c.name as category_name, g.created_at,
       (select count(*) from public.group_members m where m.group_id = g.id) as member_count,
       (select count(*) from public.group_events e where e.group_id = g.id) as event_count,
       (select m.role from public.group_members m
         where m.group_id = g.id and m.user_id = :uid) as my_role
  from public.groups g
  left join public.categories c on c.id = g.category_id
"""


async def _role(db: AsyncSession, group_id: str, user_id: str) -> Optional[str]:
    return (
        await db.execute(
            text(
                "select role from public.group_members"
                " where group_id = :g and user_id = :u"
            ),
            {"g": group_id, "u": user_id},
        )
    ).scalar_one_or_none()


async def _member(db: AsyncSession, group_id: str, user: dict) -> str:
    role = await _role(db, group_id, user["id"])
    if not role:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Join this group to see that")
    return role


async def _admin(db: AsyncSession, group_id: str, user: dict) -> str:
    role = await _role(db, group_id, user["id"])
    if role != "admin" and user["role"] not in ("admin", "super_admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Group admins only")
    return role or "admin"


async def _group(db: AsyncSession, group_id: str) -> dict:
    row = (
        await db.execute(text("select * from public.groups where id = :id"), {"id": group_id})
    ).mappings().first()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Group not found")
    return dict(row)


# ─── discovery / CRUD ───────────────────────────────────────────────────────

@router.get("")
async def list_groups(
    kind: Optional[str] = Query(None, pattern="^(circle|community)$"),
    q: Optional[str] = None,
    mine: bool = False,
    category_id: Optional[str] = None,
    university: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=100),
    user: Optional[dict] = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The Communities directory, or my Circles when `kind=circle`."""
    uid = user["id"] if user else None
    where = ["true"]
    params: dict[str, Any] = {"uid": uid, "limit": page_size, "offset": (page - 1) * page_size}

    if kind:
        where.append("g.kind = :kind")
        params["kind"] = kind
    if q:
        where.append("(g.name ilike :q or coalesce(g.description,'') ilike :q)")
        params["q"] = f"%{q}%"
    if category_id:
        where.append("g.category_id = :category_id")
        params["category_id"] = category_id
    if university:
        where.append("g.university = :university")
        params["university"] = university
    if mine:
        where.append("exists (select 1 from public.group_members m2"
                     " where m2.group_id = g.id and m2.user_id = :uid)")
    elif kind == "circle":
        # A circle is never browsable: without `mine` the list is empty.
        where.append("false")
    else:
        where.append("g.visibility in ('public','approval')")

    clause = " and ".join(where)
    rows = (
        await db.execute(
            text(GROUP_CARD + f" where {clause} order by g.created_at desc limit :limit offset :offset"),
            params,
        )
    ).mappings().all()
    total = (
        await db.execute(
            text(f"select count(*) from public.groups g where {clause}"),
            {k: v for k, v in params.items() if k not in ("limit", "offset")},
        )
    ).scalar() or 0
    return {"items": [dict(r) for r in rows], "total": total, "page": page, "page_size": page_size}


@router.post("", status_code=201)
async def create_group(
    body: GroupCreate,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Create a Circle (anyone) or a Community (admins only, per the MVP scope)."""
    if body.kind == "community" and user["role"] not in ("admin", "super_admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Communities are created by Orbit admins")
    visibility = "private" if body.kind == "circle" else body.visibility
    if visibility == "private":
        visibility = "approval"
    group_id = (
        await db.execute(
            text(
                """
                insert into public.groups
                  (kind, name, description, visibility, category_id, university, cover_url, created_by)
                values (:kind, :name, :description, :visibility,
                        cast(:category_id as uuid), :university, :cover_url, :uid)
                returning id
                """
            ),
            {
                "kind": body.kind,
                "name": body.name.strip(),
                "description": body.description,
                "visibility": visibility,
                "category_id": body.category_id,
                "university": body.university,
                "cover_url": body.cover_url,
                "uid": user["id"],
            },
        )
    ).scalar_one()
    await db.execute(
        pg_insert(GroupMember).values(group_id=group_id, user_id=user["id"], role="admin")
    )
    return await get_group(str(group_id), user, db)


@router.get("/{group_id}")
async def get_group(
    group_id: str,
    user: Optional[dict] = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """A group with its members and next shared events (members, or anyone for a public community)."""
    g = await _group(db, group_id)
    role = await _role(db, group_id, user["id"]) if user else None
    if not role and not (g["kind"] == "community" and g["visibility"] == "public"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This group is private")

    card = (
        await db.execute(
            text(GROUP_CARD + " where g.id = :id"), {"id": group_id, "uid": user["id"] if user else None}
        )
    ).mappings().first()
    members = (
        await db.execute(
            text(
                """
                select m.user_id, m.role, m.created_at, p.full_name, p.avatar_url, p.university
                  from public.group_members m
                  left join public.profiles p on p.id = m.user_id
                 where m.group_id = :id
                 order by (m.role = 'admin') desc, p.full_name nulls last
                """
            ),
            {"id": group_id},
        )
    ).mappings().all()
    events = await list_group_events(group_id, user, db) if role else {"items": []}
    return {**dict(card), "my_role": role, "members": [dict(m) for m in members], "events": events["items"]}


@router.patch("/{group_id}")
async def update_group(
    group_id: str,
    body: GroupUpdate,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Rename or re-describe a group. Admins only."""
    await _admin(db, group_id, user)
    fields = body.model_dump(exclude_none=True)
    if "visibility" in fields:
        kind = (await _group(db, group_id))["kind"]
        if kind == "circle" and fields["visibility"] != "private":
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "A Circle is always private")
    if not fields:
        return await get_group(group_id, user, db)
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    await db.execute(
        text(f"update public.groups set {sets}, updated_at = now() where id = :id"),
        {**fields, "id": group_id},
    )
    return await get_group(group_id, user, db)


@router.delete("/{group_id}", status_code=204)
async def delete_group(
    group_id: str,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a group and everything in it. Admins only."""
    await _admin(db, group_id, user)
    await db.execute(text("delete from public.groups where id = :id"), {"id": group_id})


# ─── membership ─────────────────────────────────────────────────────────────

@router.post("/{group_id}/join")
async def join_group(
    group_id: str,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Join a public community, or request to join an approval one."""
    g = await _group(db, group_id)
    if g["kind"] == "circle":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Circles are joined by invitation")
    if await _role(db, group_id, user["id"]):
        return {"status": "member"}
    if g["visibility"] == "public":
        await db.execute(
            pg_insert(GroupMember)
            .values(group_id=group_id, user_id=user["id"], role="member")
            .on_conflict_do_nothing()
        )
        return {"status": "member"}
    await db.execute(
        pg_insert(GroupInvite).values(
            group_id=group_id, invited_user_id=user["id"], invited_by=user["id"], status="pending"
        )
    )
    return {"status": "pending"}


@router.post("/{group_id}/leave", status_code=204)
async def leave_group(
    group_id: str,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Leave a community or circle. The last admin cannot leave without handing over."""
    role = await _role(db, group_id, user["id"])
    if not role:
        return
    if role == "admin":
        admins = (
            await db.execute(
                text("select count(*) from public.group_members where group_id = :g and role = 'admin'"),
                {"g": group_id},
            )
        ).scalar() or 0
        if admins <= 1:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "Make someone else an admin before you leave"
            )
    await db.execute(
        text("delete from public.group_members where group_id = :g and user_id = :u"),
        {"g": group_id, "u": user["id"]},
    )


@router.get("/{group_id}/members")
async def group_members(
    group_id: str,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Member list with profiles."""
    await _member(db, group_id, user)
    rows = (
        await db.execute(
            text(
                """
                select m.user_id, m.role, m.created_at, p.full_name, p.avatar_url,
                       p.university, p.course
                  from public.group_members m
                  left join public.profiles p on p.id = m.user_id
                 where m.group_id = :id
                 order by (m.role = 'admin') desc, p.full_name nulls last
                """
            ),
            {"id": group_id},
        )
    ).mappings().all()
    return {"items": [dict(r) for r in rows], "total": len(rows)}


@router.delete("/{group_id}/members/{user_id}", status_code=204)
async def remove_member(
    group_id: str,
    user_id: str,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Remove someone else. Admins only."""
    await _admin(db, group_id, user)
    await db.execute(
        text("delete from public.group_members where group_id = :g and user_id = :u"),
        {"g": group_id, "u": user_id},
    )


# ─── invitations ────────────────────────────────────────────────────────────

@router.post("/{group_id}/invites", status_code=201)
async def create_invite(
    group_id: str,
    body: InviteCreate,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Invite one person to a Circle, or mint a shareable join link for a Community."""
    await _admin(db, group_id, user)
    g = await _group(db, group_id)
    if body.user_id or body.email:
        lookup = body.user_id or body.email
        if body.email:
            target = (
                await db.execute(
                    text("select id from public.users where email = :e"),
                    {"e": body.email.strip().lower()},
                )
            ).scalar_one_or_none()
        else:
            target = (
                await db.execute(text("select id from public.users where id = :id"), {"id": lookup})
            ).scalar_one_or_none()
        if not target:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No Orbit account with that email yet")
        row = (
            await db.execute(
                text(
                    """
                    insert into public.group_invites (group_id, invited_user_id, invited_by)
                    values (:g, :u, :by)
                    returning id, status, expires_at, invited_user_id
                    """
                ),
                {"g": group_id, "u": target, "by": user["id"]},
            )
        ).mappings().first()
        return dict(row)

    row = (
        await db.execute(
            text(
                """
                insert into public.group_invites (group_id, code, invited_by)
                values (:g, substr(replace(gen_random_uuid()::text, '-', ''), 1, 20), :by)
                returning id, code, expires_at
                """
            ),
            {"g": group_id, "by": user["id"]},
        )
    ).mappings().first()
    return {**dict(row), "kind": g["kind"]}


@router.get("/{group_id}/invites")
async def list_invites(
    group_id: str,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Pending invites and shareable codes. Admins only."""
    await _admin(db, group_id, user)
    rows = (
        await db.execute(
            text(
                """
                select i.id, i.invited_user_id, i.code, i.status, i.expires_at, i.created_at,
                       p.full_name as invited_name, p.email as invited_email
                  from public.group_invites i
                  left join public.profiles p on p.id = i.invited_user_id
                 where i.group_id = :g
                 order by i.created_at desc
                """
            ),
            {"g": group_id},
        )
    ).mappings().all()
    return {"items": [dict(r) for r in rows], "total": len(rows)}


@router.post("/{group_id}/invites/{invite_id}/respond")
async def respond_invite(
    group_id: str,
    invite_id: str,
    body: InviteRespond,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Accept or decline an invitation (the invitee), or approve a join request (a group admin)."""
    invite = (
        await db.execute(
            text("select * from public.group_invites where id = :id and group_id = :g"),
            {"id": invite_id, "g": group_id},
        )
    ).mappings().first()
    if not invite:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invite not found")

    is_invitee = invite["invited_user_id"] and str(invite["invited_user_id"]) == user["id"]
    if not is_invitee:
        await _admin(db, group_id, user)

    new_status = "accepted" if body.accept else "declined"
    await db.execute(
        text("update public.group_invites set status = :s where id = :id"),
        {"s": new_status, "id": invite_id},
    )
    if body.accept:
        target = invite["invited_user_id"] or user["id"]
        await db.execute(
            pg_insert(GroupMember)
            .values(group_id=group_id, user_id=target, role="member")
            .on_conflict_do_nothing()
        )
    return {"status": new_status}


@router.get("/invites/{code}")
async def preview_invite(
    code: str,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """What a shareable group link points at."""
    row = (
        await db.execute(
            text(
                """
                select g.id, g.kind, g.name, g.description, g.cover_url, g.visibility,
                       i.code, i.expires_at, i.status,
                       exists (select 1 from public.group_members m
                                where m.group_id = g.id and m.user_id = :uid) as already_member
                  from public.group_invites i
                  join public.groups g on g.id = i.group_id
                 where i.code = :c and i.status = 'pending' and i.expires_at > now()
                """
            ),
            {"c": code, "uid": user["id"]},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That invite has expired")
    return dict(row)


@router.post("/invites/{code}/accept")
async def accept_invite(
    code: str,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Join a group from its shareable link."""
    invite = (
        await db.execute(
            text(
                "select * from public.group_invites"
                " where code = :c and status = 'pending' and expires_at > now()"
            ),
            {"c": code},
        )
    ).mappings().first()
    if not invite:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That invite has expired")
    await db.execute(
        text("update public.group_invites set status = 'accepted' where id = :id"),
        {"id": invite["id"]},
    )
    await db.execute(
        pg_insert(GroupMember)
        .values(group_id=invite["group_id"], user_id=user["id"], role="member")
        .on_conflict_do_nothing()
    )
    return {"group_id": str(invite["group_id"])}


# ─── shared events ──────────────────────────────────────────────────────────

@router.get("/{group_id}/events")
async def list_group_events(
    group_id: str,
    user: Optional[dict] = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Events shared with this group, soonest first, with who's interested."""
    if user:
        await _member(db, group_id, user)
    uid = user["id"] if user else None
    rows = (
        await db.execute(
            text(
                """
                select ge.event_id, ge.note, ge.created_at, ge.shared_by,
                       sp.full_name as shared_by_name,
                       e.title, e.summary, e.cover_url, e.starts_at, e.ends_at, e.timezone,
                       e.format, e.venue_name, e.city, e.external_url, e.registration_mode,
                       e.price_min, e.price_max, e.currency, e.status,
                       o.name as organiser_name, o.slug as organiser_slug,
                       (select count(*) from public.group_event_interests i
                         where i.group_id = ge.group_id and i.event_id = ge.event_id
                           and i.status in ('interested','going')) as interested_count,
                       (select count(*) from public.group_event_interests i
                         where i.group_id = ge.group_id and i.event_id = ge.event_id
                           and i.status = 'going') as going_count,
                       (select i.status from public.group_event_interests i
                         where i.group_id = ge.group_id and i.event_id = ge.event_id
                           and i.user_id = :uid) as my_interest
                  from public.group_events ge
                  join public.events e on e.id = ge.event_id
                  left join public.profiles sp on sp.id = ge.shared_by
                  left join public.organisers o on o.id = e.organiser_id
                 where ge.group_id = :g and e.starts_at >= now() - interval '6 hours'
                 order by e.starts_at
                """
            ),
            {"g": group_id, "uid": uid},
        )
    ).mappings().all()
    return {"items": [dict(r) for r in rows], "total": len(rows)}


@router.post("/{group_id}/events", status_code=201)
async def share_event(
    group_id: str,
    body: ShareEvent,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Share an event with the group. Sharing it twice is a no-op, not an error."""
    await _member(db, group_id, user)
    event = (
        await db.execute(
            text("select id, status from public.events where id = :id"), {"id": body.event_id}
        )
    ).mappings().first()
    if not event or event["status"] not in ("published", "draft"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")
    await db.execute(
        pg_insert(GroupEvent)
        .values(group_id=group_id, event_id=body.event_id, shared_by=user["id"], note=body.note)
        .on_conflict_do_nothing()
    )
    return {"group_id": group_id, "event_id": body.event_id, "shared": True}


@router.delete("/{group_id}/events/{event_id}", status_code=204)
async def unshare_event(
    group_id: str,
    event_id: str,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Remove a shared event. Whoever shared it, or a group admin."""
    row = (
        await db.execute(
            text("select shared_by from public.group_events where group_id = :g and event_id = :e"),
            {"g": group_id, "e": event_id},
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not shared with this group")
    if str(row) != user["id"]:
        await _admin(db, group_id, user)
    await db.execute(
        text("delete from public.group_events where group_id = :g and event_id = :e"),
        {"g": group_id, "e": event_id},
    )


@router.put("/{group_id}/events/{event_id}/interest")
async def set_interest(
    group_id: str,
    event_id: str,
    body: InterestBody,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """'Count me in' — interested or going."""
    await _member(db, group_id, user)
    await db.execute(
        pg_insert(GroupEventInterest)
        .values(group_id=group_id, event_id=event_id, user_id=user["id"], status=body.status)
        .on_conflict_do_update(
            index_elements=["group_id", "event_id", "user_id"],
            set_={"status": body.status},
        )
    )
    return {"status": body.status}


@router.delete("/{group_id}/events/{event_id}/interest", status_code=204)
async def clear_interest(
    group_id: str,
    event_id: str,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Take my name off an event."""
    await _member(db, group_id, user)
    await db.execute(
        text(
            "delete from public.group_event_interests"
            " where group_id = :g and event_id = :e and user_id = :u"
        ),
        {"g": group_id, "e": event_id, "u": user["id"]},
    )


@router.get("/{group_id}/events/{event_id}/interest")
async def event_interest(
    group_id: str,
    event_id: str,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Who else is in."""
    await _member(db, group_id, user)
    rows = (
        await db.execute(
            text(
                """
                select i.user_id, i.status, p.full_name, p.avatar_url
                  from public.group_event_interests i
                  left join public.profiles p on p.id = i.user_id
                 where i.group_id = :g and i.event_id = :e
                 order by (i.status = 'going') desc, p.full_name nulls last
                """
            ),
            {"g": group_id, "e": event_id},
        )
    ).mappings().all()
    return {"items": [dict(r) for r in rows], "total": len(rows)}


# ─── announcements ──────────────────────────────────────────────────────────

@router.get("/{group_id}/announcements")
async def list_announcements(
    group_id: str,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Group notices, newest first."""
    await _member(db, group_id, user)
    rows = (
        await db.execute(
            text(
                """
                select a.id, a.body, a.event_id, a.created_at, a.author_id,
                       p.full_name as author_name, e.title as event_title
                  from public.group_announcements a
                  left join public.profiles p on p.id = a.author_id
                  left join public.events e on e.id = a.event_id
                 where a.group_id = :g
                 order by a.created_at desc
                 limit 50
                """
            ),
            {"g": group_id},
        )
    ).mappings().all()
    return {"items": [dict(r) for r in rows], "total": len(rows)}


@router.post("/{group_id}/announcements", status_code=201)
async def create_announcement(
    group_id: str,
    body: AnnouncementCreate,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Post a notice. Admins only."""
    await _admin(db, group_id, user)
    row = (
        await db.execute(
            text(
                """
                insert into public.group_announcements (group_id, author_id, body, event_id)
                values (:g, :a, :b, cast(:e as uuid))
                returning id, body, event_id, created_at
                """
            ),
            {"g": group_id, "a": user["id"], "b": body.body, "e": body.event_id},
        )
    ).mappings().first()
    return dict(row)
