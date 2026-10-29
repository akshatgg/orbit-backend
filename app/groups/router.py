"""Circles (friends coordinating) and Communities (interest-based).

One resource serves both: `kind` decides the rules (see app/groups/service.py
for the access table). A Circle is private and invitation-driven; a Community
is listed in the directory and is either open (join instantly) or
approval-based. Communities are created by Orbit admins; any signed-in user can
create a Circle.

`{group_id}` accepts a group's id or, for communities, its slug.
"""

from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import current_user, require_user
from app.core.pagination import CURSOR_LIMIT_MAX
from app.core.routing import NumbersRoute
from app.groups import service as gs
from app.groups.schemas import (
    AnnouncementCreate,
    AnnouncementUpdate,
    GroupCreate,
    GroupUpdate,
    InterestBody,
    InviteCreate,
    InviteRespond,
    MemberRole,
    ShareEvent,
)
from app.notifications import inbox
from app.notifications.messages import group_invite_message
from app.users.brief import user_brief, user_columns

router = APIRouter(prefix="/groups", tags=["groups"], route_class=NumbersRoute)

INVITES_PER_HOUR = 30
LINK_PATHS = {"circle": "/join/circle/{code}", "community": "/join/community/{code}"}


def _display_name(user: dict) -> str:
    return (user.get("full_name") or "").strip() or "Someone on ORBIT"


def _admin_title(g: dict) -> tuple[str, str]:
    """What a group admin is called, bare and with its article: a community's
    moderator, a circle's admin."""
    if g["kind"] == "circle":
        return "admin", "an admin"
    return "moderator", "a moderator"


def _last_admin(g: dict) -> str:
    word, _ = _admin_title(g)
    what = "circle" if g["kind"] == "circle" else "community"
    return f"A {what} needs at least one {word}"


# ─── discovery / CRUD ───────────────────────────────────────────────────────


@router.get("")
async def list_groups(
    kind: Optional[str] = Query(None, pattern="^(circle|community)$"),
    q: Optional[str] = Query(None, max_length=100),
    mine: bool = False,
    category: Optional[str] = Query(None, max_length=100),
    category_id: Optional[str] = Query(None, max_length=100),
    university: Optional[str] = Query(None, max_length=120),
    limit: int = Query(24, ge=1, le=CURSOR_LIMIT_MAX),
    cursor: Optional[str] = Query(None, max_length=500),
    user: Optional[dict] = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The Communities directory (joined first, then by name), or my groups with `mine=1`.

    `kind=circle` always means my circles. `category` (a slug or id) matches any of a
    community's categories.
    Pages by keyset: pass `next_cursor` back as `cursor`.
    """
    return await gs.directory(
        db,
        user,
        kind=kind,
        mine=mine,
        category=category or category_id,
        university=university,
        q=q,
        limit=limit,
        cursor=cursor,
    )


@router.post("", status_code=201)
async def create_group(
    body: GroupCreate,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Create a Circle (anyone) or a Community (Orbit admins only). The creator is its admin."""
    if body.kind == "community" and not gs.is_orbit_admin(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Communities are created by Orbit admins")
    if body.kind == "circle":
        visibility, slug = "private", None
    else:
        # A community is open or approval-based; "private" means approval.
        visibility = body.visibility or "public"
        if visibility == "private":
            visibility = "approval"
        if body.slug:
            if not gs.SLUG_RE.match(body.slug):
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "A slug is lowercase letters, numbers and single dashes",
                )
            slug = await gs.unique_slug(db, body.slug)
        else:
            slug = await gs.unique_slug(db, gs.slugify(body.name))
    if body.category_ids is not None:
        category_ids = await gs.resolve_categories(db, body.category_ids)
    else:
        primary = await gs.resolve_category(db, body.category_id)
        category_ids = [primary] if primary else []
    group_id = (
        await db.execute(
            text(
                """
                insert into public.groups
                  (kind, name, slug, description, visibility, category_id, category_ids,
                   university, cover_url, allow_member_shares, created_by)
                values (:kind, :name, :slug, :description, :visibility,
                        cast(:category_id as uuid), cast(:category_ids as uuid[]),
                        :university, :cover_url, :allow_member_shares, :uid)
                returning id
                """
            ),
            {
                "kind": body.kind,
                "name": body.name.strip(),
                "slug": slug,
                "description": body.description,
                "visibility": visibility,
                "category_id": category_ids[0] if category_ids else None,
                "category_ids": category_ids,
                "university": (body.university or "").strip() or None,
                "cover_url": body.cover_url,
                "allow_member_shares": True if body.allow_member_shares is None else body.allow_member_shares,
                "uid": str(user["id"]),
            },
        )
    ).scalar_one()
    await db.execute(
        text(
            "insert into public.group_members (group_id, user_id, role) values (:g, :u, 'admin')"
        ),
        {"g": group_id, "u": str(user["id"])},
    )
    if body.kind == "circle":
        await db.execute(
            text(
                "insert into public.conversations (kind, group_id, created_by)"
                " values ('circle', :g, :u) on conflict (group_id) do nothing"
            ),
            {"g": group_id, "u": str(user["id"])},
        )
    return await get_group(str(group_id), user, db)


@router.get("/invites/mine")
async def my_invites(
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Pending invitations to me, newest first."""
    rows = (
        await db.execute(
            text(
                f"""
                select i.id, i.created_at, i.expires_at,
                       g.id as group_id, g.name as group_name, g.kind as group_kind,
                       g.slug as group_slug, g.cover_url as group_cover_url,
                       {user_columns("i.invited_by", "p", "by_")}
                  from public.group_invites i
                  join public.groups g on g.id = i.group_id
                  left join public.profiles p on p.id = i.invited_by
                 where i.invited_user_id = :u and i.kind = 'invite'
                   and i.status = 'pending' and i.expires_at > now()
                 order by i.created_at desc
                 limit 100
                """
            ),
            {"u": str(user["id"])},
        )
    ).mappings().all()
    return {
        "items": [
            {
                "id": str(r["id"]),
                "group": {
                    "id": str(r["group_id"]),
                    "name": r["group_name"],
                    "kind": r["group_kind"],
                    "slug": r["group_slug"],
                    "cover_url": r["group_cover_url"],
                },
                "invited_by": user_brief(r, "by_"),
                "created_at": r["created_at"],
                "expires_at": r["expires_at"],
            }
            for r in rows
        ]
    }


@router.get("/shareable")
async def shareable_groups(
    event_id: Optional[str] = None,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The circles and communities I can share an event to, each with `already_shared`."""
    eid = gs.as_uuid(event_id, "event id") if event_id else None
    rows = (
        await db.execute(
            text(
                """
                select g.id, g.kind, g.name, g.slug, g.cover_url, m.role as my_role,
                       (select count(*) from public.group_members mc where mc.group_id = g.id)
                         as member_count,
                       exists (select 1 from public.group_events ge
                                where ge.group_id = g.id and ge.event_id = cast(:e as uuid))
                         as already_shared
                  from public.groups g
                  left join public.group_members m on m.group_id = g.id and m.user_id = :u
                 where (m.role is not null
                        and (g.kind = 'circle' or g.allow_member_shares or m.role = 'admin'))
                    or (g.kind = 'community' and :orbit_admin)
                 order by (g.kind = 'circle') desc, lower(g.name), g.id
                """
            ),
            {"u": str(user["id"]), "e": eid, "orbit_admin": gs.is_orbit_admin(user)},
        )
    ).mappings().all()
    return {
        "items": [
            {
                "id": str(r["id"]),
                "kind": r["kind"],
                "name": r["name"],
                "slug": r["slug"],
                "cover_url": r["cover_url"],
                "my_role": r["my_role"],
                "member_count": int(r["member_count"]),
                "already_shared": bool(r["already_shared"]),
            }
            for r in rows
        ]
    }


@router.get("/invites/{code}")
async def preview_invite(
    code: str,
    user: Optional[dict] = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """What a shareable invite link points at (works signed out, so the page can ask you to sign in)."""
    row = (
        await db.execute(
            text(
                f"""
                select g.id, g.kind, g.name, g.slug, g.description, g.cover_url, g.visibility,
                       i.code, i.expires_at,
                       (select count(*) from public.group_members m where m.group_id = g.id)
                         as member_count,
                       exists (select 1 from public.group_members m
                                where m.group_id = g.id and m.user_id = cast(:uid as uuid))
                         as already_member,
                       {user_columns("i.invited_by", "p", "by_")}
                  from public.group_invites i
                  join public.groups g on g.id = i.group_id
                  left join public.profiles p on p.id = i.invited_by
                 where i.code = :c and i.kind = 'link'
                   and i.status = 'pending' and i.expires_at > now()
                """
            ),
            {"c": code, "uid": str(user["id"]) if user else None},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That invite has expired")
    return {
        "code": row["code"],
        "expires_at": row["expires_at"],
        "group": {
            "id": str(row["id"]),
            "kind": row["kind"],
            "name": row["name"],
            "slug": row["slug"],
            "description": row["description"],
            "cover_url": row["cover_url"],
            "visibility": row["visibility"],
            "member_count": int(row["member_count"]),
        },
        "invited_by": user_brief(row, "by_"),
        "already_member": bool(row["already_member"]),
    }


@router.post("/invites/{code}/accept")
async def accept_invite_link(
    code: str,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Join a group from its shareable link. The link stays valid for others until it expires."""
    invite = (
        await db.execute(
            text(
                "select i.group_id, g.kind, g.slug from public.group_invites i"
                " join public.groups g on g.id = i.group_id"
                " where i.code = :c and i.kind = 'link'"
                " and i.status = 'pending' and i.expires_at > now()"
            ),
            {"c": code},
        )
    ).mappings().first()
    if not invite:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That invite has expired")
    group_id = str(invite["group_id"])
    await _make_member(db, group_id, str(user["id"]))
    return {"group_id": group_id, "kind": invite["kind"], "slug": invite["slug"], "status": "joined"}


async def _make_member(
    db: AsyncSession, group_id: str, user_id: str, role: str = "member"
) -> None:
    """Add a member with `role`; any pending invite or request of theirs for this group is settled.

    A live admin (moderator) invite settled here still makes them an admin, and
    `role='admin'` raises someone who is already a member; nothing here demotes.
    """
    settled = (
        await db.execute(
            text(
                "update public.group_invites set status = 'accepted'"
                " where group_id = :g and invited_user_id = :u and status = 'pending'"
                " returning id, kind, role, expires_at > now() as live"
            ),
            {"g": group_id, "u": user_id},
        )
    ).mappings().all()
    if any(r["kind"] == "invite" and r["role"] == "admin" and r["live"] for r in settled):
        role = "admin"
    await db.execute(
        text(
            "insert into public.group_members as m (group_id, user_id, role) values (:g, :u, :r)"
            " on conflict (group_id, user_id) do update set role = excluded.role"
            " where excluded.role = 'admin' and m.role <> 'admin'"
        ),
        {"g": group_id, "u": user_id, "r": role},
    )
    for r in settled:
        await inbox.mark_invite_read(db, r["id"])


@router.get("/{group_id}")
async def get_group(
    group_id: str,
    user: Optional[dict] = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """A group card with `my_role`, `pending_request`, `pending_invite` and (when visible) its members."""
    g = await gs.resolve(db, group_id)
    role = await gs.role_of(db, g["id"], user)
    pending_invite = None
    if user and not role:
        pending_invite = (
            await db.execute(
                text(
                    "select id from public.group_invites where group_id = :g"
                    " and invited_user_id = :u and kind = 'invite'"
                    " and status = 'pending' and expires_at > now()"
                ),
                {"g": g["id"], "u": str(user["id"])},
            )
        ).scalar_one_or_none()
    listed = g["kind"] == "community" and g["visibility"] in ("public", "approval")
    if not (role or listed or pending_invite or (g["kind"] == "community" and gs.is_orbit_admin(user))):
        raise gs.deny(user, "This group is private")

    out = await gs.card(db, g["id"], user)
    out["pending_invite"] = {"id": str(pending_invite)} if pending_invite else None
    out["can_manage"] = gs.can_manage(g, role, user)
    out["can_invite"] = gs.can_invite(g, role, user)
    out["can_share"] = gs.can_share(g, role, user)
    out["members"] = await gs.members(db, g["id"], 100) if gs.can_read_content(g, role, user) else []
    return out


@router.patch("/{group_id}")
async def update_group(
    group_id: str,
    body: GroupUpdate,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Edit a group. Group admins; for a community, Orbit admins too."""
    g = await gs.resolve(db, group_id)
    role = await gs.role_of(db, g["id"], user)
    if not gs.can_manage(g, role, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Group admins only")

    fields: dict[str, Any] = {}
    sent = body.model_dump(exclude_unset=True)
    if sent.get("name"):
        fields["name"] = sent["name"].strip()
    if "description" in sent:
        fields["description"] = sent["description"]
    if "cover_url" in sent:
        fields["cover_url"] = sent["cover_url"]
    if "university" in sent:
        fields["university"] = (sent["university"] or "").strip() or None
    # category_id is the primary category, always category_ids' first entry.
    if "category_ids" in sent:
        ids = await gs.resolve_categories(db, sent["category_ids"] or [])
        fields["category_ids"] = ids
        fields["category_id"] = ids[0] if ids else None
    elif "category_id" in sent:
        primary = await gs.resolve_category(db, sent["category_id"])
        ids = [primary] + [c for c in g["category_ids"] if c != primary] if primary else []
        fields["category_ids"] = ids
        fields["category_id"] = primary
    if sent.get("visibility"):
        if g["kind"] == "circle":
            if sent["visibility"] != "private":
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "A Circle is always private")
        else:
            fields["visibility"] = "approval" if sent["visibility"] == "private" else sent["visibility"]
    if sent.get("allow_member_shares") is not None and g["kind"] == "community":
        fields["allow_member_shares"] = sent["allow_member_shares"]
    if sent.get("slug") and g["kind"] == "community" and sent["slug"] != g["slug"]:
        if not gs.SLUG_RE.match(sent["slug"]):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "A slug is lowercase letters, numbers and single dashes",
            )
        if await gs.unique_slug(db, sent["slug"], exclude_id=g["id"]) != sent["slug"]:
            raise HTTPException(status.HTTP_409_CONFLICT, "That slug is taken")
        fields["slug"] = sent["slug"]

    if fields:
        casts = {"category_id": "uuid", "category_ids": "uuid[]"}
        sets = ", ".join(
            f"{k} = cast(:{k} as {casts[k]})" if k in casts else f"{k} = :{k}" for k in fields
        )
        await db.execute(
            text(f"update public.groups set {sets}, updated_at = now() where id = :id"),
            {**fields, "id": g["id"]},
        )
    return await get_group(g["id"], user, db)


@router.delete("/{group_id}", status_code=204)
async def delete_group(
    group_id: str,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Delete a group and everything in it. Group admins; for a community, Orbit admins too."""
    g = await gs.resolve(db, group_id)
    role = await gs.role_of(db, g["id"], user)
    if not gs.can_manage(g, role, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Group admins only")
    await db.execute(text("delete from public.groups where id = :id"), {"id": g["id"]})
    return Response(status_code=204)


# ─── membership ─────────────────────────────────────────────────────────────


@router.post("/{group_id}/join")
async def join_group(
    group_id: str,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Join a public community (`joined`), or ask to join an approval one (`requested`).

    Someone holding an invite to the group joins straight away.
    """
    g = await gs.resolve(db, group_id)
    if await gs.role_of(db, g["id"], user):
        return {"status": "joined"}
    invited = (
        await db.execute(
            text(
                "select 1 from public.group_invites where group_id = :g and invited_user_id = :u"
                " and kind = 'invite' and status = 'pending' and expires_at > now()"
            ),
            {"g": g["id"], "u": str(user["id"])},
        )
    ).first()
    if invited:
        await _make_member(db, g["id"], str(user["id"]))
        return {"status": "joined"}
    if g["kind"] == "circle":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Circles are joined by invitation")
    if g["visibility"] == "public":
        await _make_member(db, g["id"], str(user["id"]))
        return {"status": "joined"}
    if g["visibility"] != "approval":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This community is invite-only")

    await _expire_stale(db, g["id"], str(user["id"]))
    request_id = (
        await db.execute(
            text(
                """
                insert into public.group_invites
                  (group_id, kind, invited_user_id, invited_by, expires_at)
                values (:g, 'request', :u, :u, now() + interval '90 days')
                on conflict (group_id, invited_user_id)
                  where status = 'pending' and invited_user_id is not null
                  do nothing
                returning id
                """
            ),
            {"g": g["id"], "u": str(user["id"])},
        )
    ).scalar_one_or_none()
    if request_id:
        await inbox.notify_group(
            db,
            g["id"],
            "join_request",
            f"{_display_name(user)} asked to join {g['name']}",
            admins_only=True,
            body="Approve or decline their request.",
            link=gs.group_path(g),
            actor_id=str(user["id"]),
            invite_id=request_id,
        )
    return {"status": "requested"}


async def _expire_stale(db: AsyncSession, group_id: str, user_id: str) -> None:
    """A pending row past its expiry no longer counts; revoke it so a new one can be made."""
    await db.execute(
        text(
            "update public.group_invites set status = 'revoked'"
            " where group_id = :g and invited_user_id = :u and status = 'pending'"
            " and expires_at <= now()"
        ),
        {"g": group_id, "u": user_id},
    )


@router.post("/{group_id}/leave", status_code=204)
async def leave_group(
    group_id: str,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Leave a group. The last admin of a group with other members hands over first;
    the last member of a circle leaving deletes it."""
    g = await gs.resolve(db, group_id)
    role = await gs.role_of(db, g["id"], user)
    if not role:
        return Response(status_code=204)
    if role == "admin" and await gs.admin_count(db, g["id"]) <= 1:
        others = (
            await db.execute(
                text(
                    "select count(*) from public.group_members where group_id = :g and user_id <> :u"
                ),
                {"g": g["id"], "u": str(user["id"])},
            )
        ).scalar() or 0
        if others == 0 and g["kind"] == "circle":
            await db.execute(text("delete from public.groups where id = :g"), {"g": g["id"]})
            return Response(status_code=204)
        if others and not (g["kind"] == "community" and gs.is_orbit_admin(user)):
            raise HTTPException(
                status.HTTP_409_CONFLICT, "Make someone else an admin before you leave"
            )
    await db.execute(
        text("delete from public.group_members where group_id = :g and user_id = :u"),
        {"g": g["id"], "u": str(user["id"])},
    )
    await gs.drop_membership_traces(db, g["id"], str(user["id"]))
    return Response(status_code=204)


@router.get("/{group_id}/members")
async def group_members(
    group_id: str,
    user: Optional[dict] = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Members, admins first: `{user, role, joined_at}`."""
    g = await gs.resolve(db, group_id)
    role = await gs.role_of(db, g["id"], user)
    if not gs.can_read_content(g, role, user):
        raise gs.deny(user, "Join this group to see its members")
    items = await gs.members(db, g["id"])
    return {"items": items, "total": len(items)}


async def _target_role(db: AsyncSession, group_id: str, user_id: str) -> str:
    role = (
        await db.execute(
            text("select role from public.group_members where group_id = :g and user_id = :u"),
            {"g": group_id, "u": user_id},
        )
    ).scalar_one_or_none()
    if not role:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not a member of this group")
    return role


@router.delete("/{group_id}/members/{user_id}", status_code=204)
async def remove_member(
    group_id: str,
    user_id: UUID,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Remove a member. Group admins (communities: Orbit admins too). Never the last admin."""
    g = await gs.resolve(db, group_id)
    if not gs.can_manage(g, await gs.role_of(db, g["id"], user), user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Group admins only")
    target = str(user_id)
    if await _target_role(db, g["id"], target) == "admin" and await gs.admin_count(db, g["id"]) <= 1:
        raise HTTPException(status.HTTP_409_CONFLICT, _last_admin(g))
    await db.execute(
        text("delete from public.group_members where group_id = :g and user_id = :u"),
        {"g": g["id"], "u": target},
    )
    await gs.drop_membership_traces(db, g["id"], target)
    return Response(status_code=204)


@router.patch("/{group_id}/members/{user_id}")
async def set_member_role(
    group_id: str,
    user_id: UUID,
    body: MemberRole,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Make a member an admin (a community moderator) or back to member."""
    g = await gs.resolve(db, group_id)
    if not gs.can_manage(g, await gs.role_of(db, g["id"], user), user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Group admins only")
    target = str(user_id)
    current = await _target_role(db, g["id"], target)
    if current == "admin" and body.role == "member" and await gs.admin_count(db, g["id"]) <= 1:
        raise HTTPException(status.HTTP_409_CONFLICT, _last_admin(g))
    await db.execute(
        text("update public.group_members set role = :r where group_id = :g and user_id = :u"),
        {"r": body.role, "g": g["id"], "u": target},
    )
    row = next(m for m in await gs.members(db, g["id"]) if m["user"]["id"] == target)
    return row


# ─── invitations ────────────────────────────────────────────────────────────


@router.post("/{group_id}/invites", status_code=201)
async def create_invite(
    group_id: str,
    body: InviteCreate,
    response: Response,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Invite someone who has an Orbit account, by email (or user id).

    Circles: any member. Communities: group admins and Orbit admins.
    `role: "admin"` (group admins; Orbit admins for communities) invites them as a
    moderator (a circle admin): a member is promoted straight away (200,
    `status: "promoted"`), and a pending invite of theirs is upgraded.
    409 when they're already a member (or already an admin) or already invited.
    """
    g = await gs.resolve(db, group_id)
    role = await gs.role_of(db, g["id"], user)
    admin_word, an_admin = _admin_title(g)
    if body.role == "admin" and not gs.can_manage(g, role, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Only {admin_word}s can add {admin_word}s")
    if not gs.can_invite(g, role, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You can't invite people to this group")
    if not (body.email or body.user_id):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Give an email address")

    recent = (
        await db.execute(
            text(
                "select count(*) from public.group_invites where invited_by = :u"
                " and kind = 'invite' and created_at > now() - interval '1 hour'"
            ),
            {"u": str(user["id"])},
        )
    ).scalar() or 0
    if recent >= INVITES_PER_HOUR:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "You've sent a lot of invites — try again later",
            headers={"Retry-After": "600"},
        )

    if body.email:
        target = (
            await db.execute(
                text("select id from public.users where email = :e"),
                {"e": body.email.strip().lower()},
            )
        ).scalar_one_or_none()
    else:
        target = (
            await db.execute(
                text("select id from public.users where id = :id"),
                {"id": gs.as_uuid(body.user_id, "user id")},
            )
        ).scalar_one_or_none()
    if not target:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No Orbit account with that email yet")
    target = str(target)
    if target == str(user["id"]):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "You're already in this group")
    inviter = _display_name(user)

    target_role = await gs.role_of(db, g["id"], {"id": target})
    if target_role:
        if body.role != "admin":
            raise HTTPException(status.HTTP_409_CONFLICT, "They're already a member")
        promoted = (
            await db.execute(
                text(
                    "update public.group_members set role = 'admin'"
                    " where group_id = :g and user_id = :t and role <> 'admin' returning user_id"
                ),
                {"g": g["id"], "t": target},
            )
        ).first()
        if not promoted:
            raise HTTPException(status.HTTP_409_CONFLICT, f"They're already {an_admin}")
        what = "circle" if g["kind"] == "circle" else "community"
        await inbox.notify(
            db,
            [target],
            "request_approved",
            f"You're now {an_admin} of {g['name']}",
            body=f"{inviter} made you {an_admin}, so you can help run the {what}.",
            link=gs.group_path(g),
            actor_id=str(user["id"]),
            group_id=g["id"],
        )
        response.status_code = status.HTTP_200_OK
        return {
            "status": "promoted",
            "group_id": g["id"],
            "user": await _user_brief(db, target),
        }

    await _expire_stale(db, g["id"], target)
    returning = "returning id, status, role, created_at, expires_at"
    row = (
        await db.execute(
            text(
                f"""
                insert into public.group_invites
                  (group_id, kind, invited_user_id, invited_by, role)
                values (:g, 'invite', :t, :by, :role)
                on conflict (group_id, invited_user_id)
                  where status = 'pending' and invited_user_id is not null
                  do nothing
                {returning}
                """
            ),
            {"g": g["id"], "t": target, "by": str(user["id"]), "role": body.role},
        )
    ).mappings().first()
    if not row:
        existing = (
            await db.execute(
                text(
                    "select id, kind, role from public.group_invites where group_id = :g"
                    " and invited_user_id = :t and status = 'pending'"
                ),
                {"g": g["id"], "t": target},
            )
        ).mappings().first()
        if existing and existing["kind"] == "request":
            raise HTTPException(
                status.HTTP_409_CONFLICT, "They've asked to join already — approve their request"
            )
        if existing and body.role == "admin" and existing["role"] != "admin":
            # Upgrade their pending invite; the new notification replaces the old one.
            row = (
                await db.execute(
                    text(
                        "update public.group_invites set role = 'admin', invited_by = :by"
                        f" where id = :id and status = 'pending' {returning}"
                    ),
                    {"id": existing["id"], "by": str(user["id"])},
                )
            ).mappings().first()
            if row:
                await db.execute(
                    text(
                        "delete from public.notifications where invite_id = :i and user_id = :t"
                    ),
                    {"i": existing["id"], "t": target},
                )
        if not row:
            raise HTTPException(status.HTTP_409_CONFLICT, "They're already invited")

    path = "/circles" if g["kind"] == "circle" else gs.group_path(g)
    message = group_invite_message(g, inviter, path, row["role"])
    await inbox.notify(
        db,
        [target],
        "circle_invite",
        message.title,
        body=message.body,
        link=path,
        actor_id=str(user["id"]),
        group_id=g["id"],
        invite_id=row["id"],
        deliver=message,
    )
    return {
        "id": str(row["id"]),
        "kind": "invite",
        "status": row["status"],
        "role": row["role"],
        "group_id": g["id"],
        "user": await _user_brief(db, target),
        "invited_by": {
            "id": str(user["id"]),
            "full_name": user.get("full_name"),
            "avatar_url": user.get("avatar_url"),
            "university": user.get("university"),
        },
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
    }


async def _user_brief(db: AsyncSession, user_id: str) -> Optional[dict]:
    row = (
        await db.execute(
            text(
                f"select {user_columns('u.id', 'p', 'u_')} from public.users u"
                " left join public.profiles p on p.id = u.id where u.id = :t"
            ),
            {"t": user_id},
        )
    ).mappings().first()
    return user_brief(row, "u_") if row else None


@router.get("/{group_id}/invites")
async def list_invites(
    group_id: str,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Pending invites and join requests (`kind: invite|request`), newest first. Group admins.

    `role` is what accepting makes them: 'admin' for a moderator invite, else 'member'.
    """
    g = await gs.resolve(db, group_id)
    if not gs.can_manage(g, await gs.role_of(db, g["id"], user), user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Group admins only")
    rows = (
        await db.execute(
            text(
                f"""
                select i.id, i.kind, i.status, i.role, i.created_at, i.expires_at,
                       {user_columns("i.invited_user_id", "p", "u_")},
                       {user_columns("i.invited_by", "bp", "by_")}
                  from public.group_invites i
                  left join public.profiles p on p.id = i.invited_user_id
                  left join public.profiles bp on bp.id = i.invited_by
                 where i.group_id = :g and i.kind in ('invite', 'request')
                   and i.status = 'pending' and i.expires_at > now()
                 order by i.created_at desc
                 limit 500
                """
            ),
            {"g": g["id"]},
        )
    ).mappings().all()
    items = [
        {
            "id": str(r["id"]),
            "kind": r["kind"],
            "status": r["status"],
            "role": r["role"],
            "user": user_brief(r, "u_"),
            "invited_by": user_brief(r, "by_") if r["kind"] == "invite" else None,
            "created_at": r["created_at"],
            "expires_at": r["expires_at"],
        }
        for r in rows
    ]
    return {"items": items, "total": len(items)}


@router.post("/{group_id}/invites/{invite_id}/respond")
async def respond_invite(
    group_id: str,
    invite_id: UUID,
    body: InviteRespond,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Answer an invite (only the invitee) or a join request (only a group admin).

    Accepting an invite makes them a member with the invite's role.

    The requester may withdraw their own request (`accept: false`). 409 when it was
    already answered, 410 when it has expired.
    """
    g = await gs.resolve(db, group_id)
    invite = (
        await db.execute(
            text(
                "select id, kind, status, role, invited_user_id, expires_at > now() as live"
                " from public.group_invites where id = :id and group_id = :g"
            ),
            {"id": str(invite_id), "g": g["id"]},
        )
    ).mappings().first()
    if not invite or invite["kind"] == "link":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invite not found")
    if invite["status"] != "pending":
        raise HTTPException(status.HTTP_409_CONFLICT, f"This was already {invite['status']}")
    if not invite["live"]:
        raise HTTPException(status.HTTP_410_GONE, "This invite has expired")

    subject = str(invite["invited_user_id"])
    is_subject = subject == str(user["id"])
    if invite["kind"] == "invite":
        if not is_subject:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the person invited can answer")
        new_status = "accepted" if body.accept else "declined"
    elif is_subject:
        if body.accept:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "A group admin has to approve your request")
        new_status = "revoked"
    else:
        if not gs.can_manage(g, await gs.role_of(db, g["id"], user), user):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Group admins only")
        new_status = "accepted" if body.accept else "declined"

    updated = (
        await db.execute(
            text(
                "update public.group_invites set status = :s"
                " where id = :id and status = 'pending' returning id"
            ),
            {"s": new_status, "id": str(invite_id)},
        )
    ).first()
    if not updated:
        raise HTTPException(status.HTTP_409_CONFLICT, "This was already answered")
    await inbox.mark_invite_read(db, invite_id)
    if new_status == "accepted":
        # An invite makes them what it says (a moderator invite: an admin); a request, a member.
        role = invite["role"] if invite["kind"] == "invite" else "member"
        await _make_member(db, g["id"], subject, role)
        if invite["kind"] == "request":
            await inbox.notify(
                db,
                [subject],
                "request_approved",
                f"You're in {g['name']}",
                body="Your request to join was approved.",
                link=gs.group_path(g),
                actor_id=str(user["id"]),
                group_id=g["id"],
            )
    return {"status": new_status, "group_id": g["id"]}


@router.post("/{group_id}/invite-link")
async def invite_link(
    group_id: str,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """A shareable join link (reused while it has more than a day left; 14 days from minting).

    `url_path` is the frontend page for it: `/join/circle/{code}` or `/join/community/{code}`.
    Anyone signed in who opens it joins as a member, even an approval community.
    """
    g = await gs.resolve(db, group_id)
    if not gs.can_invite(g, await gs.role_of(db, g["id"], user), user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You can't invite people to this group")
    row = (
        await db.execute(
            text(
                "select code, expires_at from public.group_invites where group_id = :g"
                " and kind = 'link' and status = 'pending'"
                " and expires_at > now() + interval '1 day'"
                " order by expires_at desc limit 1"
            ),
            {"g": g["id"]},
        )
    ).mappings().first()
    if not row:
        row = (
            await db.execute(
                text(
                    """
                    insert into public.group_invites (group_id, kind, code, invited_by)
                    values (:g, 'link', encode(gen_random_bytes(12), 'hex'), :by)
                    returning code, expires_at
                    """
                ),
                {"g": g["id"], "by": str(user["id"])},
            )
        ).mappings().first()
    return {
        "code": row["code"],
        "url_path": LINK_PATHS[g["kind"]].format(code=row["code"]),
        "expires_at": row["expires_at"],
    }


# ─── shared events ──────────────────────────────────────────────────────────


@router.get("/{group_id}/events")
async def list_group_events(
    group_id: str,
    when: str = Query("upcoming", pattern="^(upcoming|past)$"),
    limit: int = Query(20, ge=1, le=CURSOR_LIMIT_MAX),
    cursor: Optional[str] = Query(None, max_length=500),
    user: Optional[dict] = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The group's event feed: upcoming soonest first, or past newest first.

    Shared events carry `shared_by`; a community's upcoming feed also picks up
    published events in its category (and university) with `shared_by: null`.
    Up to 8 interested/going members per event, with full counts.
    Pages by keyset: pass `next_cursor` back as `cursor`.
    """
    g = await gs.resolve(db, group_id)
    role = await gs.role_of(db, g["id"], user)
    if not gs.can_read_content(g, role, user):
        raise gs.deny(user, "Join this group to see its events")
    show_people = bool(role) or (g["kind"] == "community" and gs.is_orbit_admin(user))
    return await gs.feed(
        db, g, user, when=when, limit=limit, cursor=cursor, show_people=show_people
    )


@router.post("/{group_id}/events", status_code=201)
async def share_event(
    group_id: str,
    body: ShareEvent,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Share a published, upcoming event with the group. Sharing it twice is a no-op."""
    g = await gs.resolve(db, group_id)
    role = await gs.role_of(db, g["id"], user)
    if not gs.can_share(g, role, user):
        if role or (g["kind"] == "community" and gs.is_orbit_admin(user)):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Only admins can share to this community")
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Join this group to share with it")
    event_id = gs.as_uuid(body.event_id, "event id")
    event = (
        await db.execute(
            text(
                f"""
                select e.id, e.title,
                       e.status = 'published' and {gs.UPCOMING} and {gs.EVENT_VISIBLE} as shareable
                  from public.events e
                  left join public.organisers o on o.id = e.organiser_id
                 where e.id = :id
                """
            ),
            {"id": event_id},
        )
    ).mappings().first()
    if not event:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")
    if not event["shareable"]:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "Only published, upcoming events can be shared"
        )
    inserted = (
        await db.execute(
            text(
                "insert into public.group_events (group_id, event_id, shared_by, note)"
                " values (:g, :e, :u, :n) on conflict do nothing returning event_id"
            ),
            {"g": g["id"], "e": event_id, "u": str(user["id"]), "n": body.note},
        )
    ).first()
    if inserted and g["kind"] == "circle":
        await inbox.notify_group(
            db,
            g["id"],
            "event_shared",
            f"{_display_name(user)} shared an event in {g['name']}",
            body=event["title"],
            link=gs.group_path(g),
            actor_id=str(user["id"]),
        )
    return {
        "group_id": g["id"],
        "event_id": event_id,
        "shared": True,
        "already_shared": inserted is None,
    }


@router.delete("/{group_id}/events/{event_id}", status_code=204)
async def unshare_event(
    group_id: str,
    event_id: UUID,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Remove a shared event. Whoever shared it, or a group admin."""
    g = await gs.resolve(db, group_id)
    eid = str(event_id)
    row = (
        await db.execute(
            text(
                "select shared_by from public.group_events where group_id = :g and event_id = :e"
            ),
            {"g": g["id"], "e": eid},
        )
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not shared with this group")
    if str(row[0]) != str(user["id"]) and not gs.can_manage(g, await gs.role_of(db, g["id"], user), user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only whoever shared it, or an admin")
    await db.execute(
        text("delete from public.group_events where group_id = :g and event_id = :e"),
        {"g": g["id"], "e": eid},
    )
    if not await gs.in_feed(db, g, eid):
        await db.execute(
            text("delete from public.group_event_interests where group_id = :g and event_id = :e"),
            {"g": g["id"], "e": eid},
        )
    return Response(status_code=204)


async def _interest_target(db: AsyncSession, group_id: str, event_id: UUID, user: dict) -> tuple[dict, str]:
    g = await gs.resolve(db, group_id)
    if not await gs.role_of(db, g["id"], user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Join this group first")
    eid = str(event_id)
    if not await gs.in_feed(db, g, eid):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That event isn't in this group")
    return g, eid


@router.put("/{group_id}/events/{event_id}/interest")
async def set_interest(
    group_id: str,
    event_id: UUID,
    body: InterestBody,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """'I'm interested' or 'Count me in' (going)."""
    g, eid = await _interest_target(db, group_id, event_id, user)
    await db.execute(
        text(
            """
            insert into public.group_event_interests (group_id, event_id, user_id, status)
            values (:g, :e, :u, :s)
            on conflict (group_id, event_id, user_id) do update set status = excluded.status
            """
        ),
        {"g": g["id"], "e": eid, "u": str(user["id"]), "s": body.status},
    )
    return {"status": body.status}


@router.delete("/{group_id}/events/{event_id}/interest", status_code=204)
async def clear_interest(
    group_id: str,
    event_id: UUID,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Take my name off an event."""
    g = await gs.resolve(db, group_id)
    await db.execute(
        text(
            "delete from public.group_event_interests"
            " where group_id = :g and event_id = :e and user_id = :u"
        ),
        {"g": g["id"], "e": str(event_id), "u": str(user["id"])},
    )
    return Response(status_code=204)


@router.get("/{group_id}/events/{event_id}/interest")
async def event_interest(
    group_id: str,
    event_id: UUID,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Everyone in the group who marked this event, going first."""
    g = await gs.resolve(db, group_id)
    if not await gs.role_of(db, g["id"], user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Join this group first")
    rows = (
        await db.execute(
            text(
                f"""
                select i.status, {user_columns("i.user_id", "p", "u_")}
                  from public.group_event_interests i
                  join public.group_members m on m.group_id = i.group_id and m.user_id = i.user_id
                  left join public.profiles p on p.id = i.user_id
                 where i.group_id = :g and i.event_id = :e
                 order by (i.status = 'going') desc, i.created_at
                """
            ),
            {"g": g["id"], "e": str(event_id)},
        )
    ).mappings().all()
    items = [{"user": user_brief(r, "u_"), "status": r["status"]} for r in rows]
    return {"items": items, "total": len(items)}


# ─── announcements ──────────────────────────────────────────────────────────

ANNOUNCEMENT_SELECT = f"""
select a.id, a.body, a.event_id, a.created_at, a.edited_at, e.title as event_title,
       {user_columns("a.author_id", "p", "a_")}
  from public.group_announcements a
  left join public.profiles p on p.id = a.author_id
  left join public.events e on e.id = a.event_id
"""


def _announcement(r: Any, *, manager: bool, me: str) -> dict:
    """An announcement, with whether I may edit/delete it (its author, a group admin,
    or for a community an Orbit admin)."""
    allowed = manager or (r["a_id"] is not None and str(r["a_id"]) == me)
    return {
        "id": str(r["id"]),
        "body": r["body"],
        "event_id": str(r["event_id"]) if r["event_id"] else None,
        "event_title": r["event_title"],
        "author": user_brief(r, "a_"),
        "created_at": r["created_at"],
        "edited_at": r["edited_at"],
        "can_edit": allowed,
        "can_delete": allowed,
    }


@router.get("/{group_id}/announcements")
async def list_announcements(
    group_id: str,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Group notices, newest first (the latest 50). Members (communities: Orbit admins too)."""
    g = await gs.resolve(db, group_id)
    role = await gs.role_of(db, g["id"], user)
    if not (role or (g["kind"] == "community" and gs.is_orbit_admin(user))):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Join this group to read its announcements")
    rows = (
        await db.execute(
            text(ANNOUNCEMENT_SELECT + " where a.group_id = :g order by a.created_at desc limit 50"),
            {"g": g["id"]},
        )
    ).mappings().all()
    manager = gs.can_manage(g, role, user)
    items = [_announcement(r, manager=manager, me=str(user["id"])) for r in rows]
    return {"items": items, "total": len(items)}


@router.post("/{group_id}/announcements", status_code=201)
async def create_announcement(
    group_id: str,
    body: AnnouncementCreate,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Post a notice; every other member gets it in their inbox. Group admins."""
    g = await gs.resolve(db, group_id)
    if not gs.can_manage(g, await gs.role_of(db, g["id"], user), user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Group admins only")
    event_id = gs.as_uuid(body.event_id, "event id") if body.event_id else None
    announcement_id = (
        await db.execute(
            text(
                "insert into public.group_announcements (group_id, author_id, body, event_id)"
                " values (:g, :a, :b, cast(:e as uuid)) returning id"
            ),
            {"g": g["id"], "a": str(user["id"]), "b": body.body.strip(), "e": event_id},
        )
    ).scalar_one()
    await inbox.notify_group(
        db,
        g["id"],
        "announcement",
        f"New in {g['name']}",
        body=body.body.strip()[:200],
        link=gs.group_path(g),
        actor_id=str(user["id"]),
        announcement_id=announcement_id,
    )
    return await _announcement_out(db, g, str(announcement_id), user)


async def _announcement_out(db: AsyncSession, g: dict, announcement_id: str, user: dict) -> dict:
    row = (
        await db.execute(text(ANNOUNCEMENT_SELECT + " where a.id = :id"), {"id": announcement_id})
    ).mappings().first()
    manager = gs.can_manage(g, await gs.role_of(db, g["id"], user), user)
    return _announcement(row, manager=manager, me=str(user["id"]))


async def _editable_announcement(
    db: AsyncSession, group_id: str, announcement_id: UUID, user: dict
) -> tuple[dict, str]:
    """The group and announcement if I may change it: 404 if it isn't in this group,
    403 unless I wrote it, I'm a group admin, or (a community) I'm an Orbit admin."""
    g = await gs.resolve(db, group_id)
    author = (
        await db.execute(
            text(
                "select author_id from public.group_announcements where id = :id and group_id = :g"
            ),
            {"id": str(announcement_id), "g": g["id"]},
        )
    ).first()
    if author is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Announcement not found")
    mine = author[0] is not None and str(author[0]) == str(user["id"])
    if not (mine or gs.can_manage(g, await gs.role_of(db, g["id"], user), user)):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Only its author or a group admin can change this"
        )
    return g, str(announcement_id)


@router.patch("/{group_id}/announcements/{announcement_id}")
async def update_announcement(
    group_id: str,
    announcement_id: UUID,
    body: AnnouncementUpdate,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Edit an announcement's text. Its author, a group admin, or (communities) an Orbit admin."""
    g, aid = await _editable_announcement(db, group_id, announcement_id, user)
    await db.execute(
        text("update public.group_announcements set body = :b, edited_at = now() where id = :id"),
        {"b": body.body, "id": aid},
    )
    # Inbox entries quote the announcement: keep them current.
    await db.execute(
        text("update public.notifications set body = :b where announcement_id = :id"),
        {"b": body.body[:200], "id": aid},
    )
    return await _announcement_out(db, g, aid, user)


@router.delete("/{group_id}/announcements/{announcement_id}", status_code=204)
async def delete_announcement(
    group_id: str,
    announcement_id: UUID,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Delete an announcement and its inbox notifications. Same people as editing."""
    _, aid = await _editable_announcement(db, group_id, announcement_id, user)
    await db.execute(text("delete from public.group_announcements where id = :id"), {"id": aid})
    return Response(status_code=204)
