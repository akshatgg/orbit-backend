"""Circles, Communities & Channels: lookups, access rules and the shapes the API returns.

Who may do what (the router enforces it through these helpers):

| | Circle | Community | Channel |
|---|---|---|---|
| see the group card | members (and people it invited) | anyone if public/approval; members if private | as its community, if public/approval; private: members, the invited and managers |
| see members | members | members; public: anyone | members and managers |
| see events | members | members; public: anyone | members and managers; public: as its community |
| read the chat | members | no chat | members (a manager joins first) |
| post in the chat | members | no chat | members; `post_policy='moderators'`: managers |
| read announcements | members | members (and Orbit admins) | members (and managers) |
| manage (edit, invites list, roles, delete) | group admins | group admins, its organisation's team and Orbit admins | its admins, its community's admins and organisation's team, and Orbit admins |
| invite by email / mint a link | any member | group admins and Orbit admins | managers |
| share an event | any member | members if `allow_member_shares`, else admins | as a community, and only managers under `post_policy='moderators'` |

Orbit admins get no special access to Circles: they are private friend groups.

A community an organisation runs (`organiser_id`) is managed by that organisation's team too
(its owners and editors, while it's approved), and so are its channels: `org_role`.

Joining a channel joins its community. Leaving or being removed from a community leaves
all its channels. An `auto_join` channel can't be left on its own.

`auto_join` must not make everyone reachable: the member list of an `auto_join` group is
for those who can manage it (everyone else gets the count), and such a group doesn't count
as sharing a group for direct messages (`app.messaging.service.share_a_group`).

Every list here is a fixed number of set-based queries (no query per row), and
pages by keyset (`next_cursor`), never OFFSET.
"""

import re
from typing import Any, Optional
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import decode_cursor, encode_cursor
from app.events.models import Event
from app.events.service import card as event_card
from app.events.service import card_query, escape_like, with_counts
from app.users.brief import user_brief, user_columns

ORBIT_ADMIN_ROLES = ("admin", "super_admin")
# Orbit admins aren't held to it.
COMMUNITIES_PER_ORGANISER = 3
SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
AVATARS_PER_EVENT = 8

# An event anyone may open (the events_select rule) over `events e left join organisers o`.
EVENT_VISIBLE = "(e.organiser_id is null or o.status = 'approved')"
UPCOMING = "coalesce(e.ends_at, e.starts_at) >= now()"
LISTED = ("public", "approval")


def org_team_join(group: str, parent: str, uid: str) -> str:
    """Joins `org_team.role`: the caller's role in the approved organisation that runs this
    community (or this channel's community), else null. `group` and `parent` are aliases of
    the group and its community, `uid` the caller's id as SQL."""
    return f"""
      left join lateral (
        select om.role from public.organiser_members om
          join public.organisers org on org.id = om.organiser_id and org.status = 'approved'
         where om.organiser_id = coalesce({group}.organiser_id, {parent}.organiser_id)
           and om.user_id = {uid}
      ) org_team on true"""

# Events a community picks up by itself: published, not a duplicate, in one of its
# categories and (when set) its university. Bound as :categories / :university from the group.
MATCHED_REST = """
    e.status = 'published' and e.duplicate_of is null
    and (cast(:university as text) is null or lower(e.university) = lower(cast(:university as text)))
"""
MATCHED = "e.category_id = any(cast(:categories as uuid[])) and " + MATCHED_REST

# The card's computed columns, for rows of `p` (a page of groups that already carries
# `my_role`). Each lateral runs once per row of the page, on an index.
CARD_SELECT = f"""
select p.id, p.kind, p.name, p.slug, p.description, p.cover_url, p.university,
       p.visibility, p.category_id, c.name as category_name, c.slug as category_slug,
       p.allow_member_shares, p.created_by, p.created_at, p.my_role,
       p.parent_id, parent.slug as parent_slug, parent.name as parent_name,
       p.post_policy, p.is_announcements, p.auto_join,
       org.id as org_id, org.slug as org_slug, org.name as org_name, org.logo_url as org_logo_url,
       org.is_verified as org_is_verified,
       mc.n as member_count,
       shared.n + matched.n as upcoming_event_count,
       pr.pending as pending_request,
       cats.ids as cat_ids, cats.slugs as cat_slugs, cats.names as cat_names
  from page p
  left join public.categories c on c.id = p.category_id
  left join public.groups parent on parent.id = p.parent_id
  left join public.organisers org
    on org.id = coalesce(p.organiser_id, parent.organiser_id) and org.status = 'approved'
  cross join lateral (
    select count(*) as n from public.group_members m where m.group_id = p.id
  ) mc
  cross join lateral (
    select count(*) as n
      from public.group_events ge
      join public.events e on e.id = ge.event_id
      left join public.organisers o on o.id = e.organiser_id
     where ge.group_id = p.id and e.status = 'published' and {UPCOMING} and {EVENT_VISIBLE}
  ) shared
  cross join lateral (
    select count(*) as n
      from public.events e
      left join public.organisers o on o.id = e.organiser_id
     where p.kind <> 'circle' and e.category_id = any(p.category_ids)
       and e.status = 'published' and e.duplicate_of is null
       and (p.university is null or lower(e.university) = lower(p.university))
       and {UPCOMING} and {EVENT_VISIBLE}
       and not exists (select 1 from public.group_events ge
                        where ge.group_id = p.id and ge.event_id = e.id)
  ) matched
  cross join lateral (
    select exists (
      select 1 from public.group_invites i
       where i.group_id = p.id and i.invited_user_id = cast(:uid as uuid)
         and i.kind = 'request' and i.status = 'pending' and i.expires_at > now()
    ) as pending
  ) pr
  cross join lateral (
    select array_agg(c2.id order by x.n) as ids, array_agg(c2.slug order by x.n) as slugs,
           array_agg(c2.name order by x.n) as names
      from unnest(p.category_ids) with ordinality as x(id, n)
      join public.categories c2 on c2.id = x.id
  ) cats
"""


def is_orbit_admin(user: Optional[dict]) -> bool:
    return bool(user) and user["role"] in ORBIT_ADMIN_ROLES


def group_out(row: Any) -> dict[str, Any]:
    """The group card every endpoint returns. `organiser` is the organisation that runs a
    community (`parent_organiser` its channels'), while it's approved."""
    r = dict(row)
    organiser = (
        {
            "id": str(r["org_id"]),
            "slug": r["org_slug"],
            "name": r["org_name"],
            "logo_url": r["org_logo_url"],
            "is_verified": bool(r["org_is_verified"]),
        }
        if r["org_id"]
        else None
    )
    return {
        "id": str(r["id"]),
        "kind": r["kind"],
        "name": r["name"],
        "slug": r["slug"],
        "description": r["description"],
        "visibility": r["visibility"],
        "category_id": str(r["category_id"]) if r["category_id"] else None,
        "category_name": r["category_name"],
        "category_slug": r["category_slug"],
        "categories": [
            {"id": str(i), "slug": slug, "name": name}
            for i, slug, name in zip(r["cat_ids"] or [], r["cat_slugs"] or [], r["cat_names"] or [])
        ],
        "university": r["university"],
        "cover_url": r["cover_url"],
        "allow_member_shares": r["allow_member_shares"],
        "member_count": int(r["member_count"] or 0),
        "upcoming_event_count": int(r["upcoming_event_count"] or 0),
        "my_role": r["my_role"],
        "joined": r["my_role"] is not None,
        "pending_request": bool(r["pending_request"]),
        "created_by": str(r["created_by"]) if r["created_by"] else None,
        "created_at": r["created_at"],
        "parent_id": str(r["parent_id"]) if r["parent_id"] else None,
        "parent_slug": r["parent_slug"],
        "parent_name": r["parent_name"],
        "post_policy": r["post_policy"],
        "is_announcements": r["is_announcements"],
        "auto_join": r["auto_join"],
        "organiser": organiser if r["kind"] == "community" else None,
        "parent_organiser": organiser if r["kind"] == "channel" else None,
    }


def group_path(g: dict) -> str:
    """Where the frontend shows a group."""
    if g["kind"] == "circle":
        return f"/circles/{g['id']}"
    if g["kind"] == "channel":
        return f"/communities/{g['parent_slug']}/{g['slug']}"
    return f"/communities/{g.get('slug') or g['id']}"


def as_uuid(value: Any, what: str = "id") -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Not a valid {what}") from None


# ─── lookups ────────────────────────────────────────────────────────────────


async def resolve(db: AsyncSession, ref: str, user: Optional[dict] = None) -> dict:
    """A group by id, or by slug (communities and channels). 404 when neither matches.

    A channel comes with its community's slug, name and visibility, and `parent_role`:
    `user`'s role in the community. `org_role` is `user`'s role in the organisation that runs
    the community. `can_manage` reads both.
    """
    try:
        key, value = "id", str(UUID(ref))
    except ValueError:
        key, value = "slug", ref.strip().lower()
    row = (
        await db.execute(
            text(
                f"""
                select g.*, parent.slug as parent_slug, parent.name as parent_name,
                       parent.visibility as parent_visibility, pm.role as parent_role,
                       org_team.role as org_role
                  from public.groups g
                  left join public.groups parent on parent.id = g.parent_id
                  left join public.group_members pm
                    on pm.group_id = g.parent_id and pm.user_id = cast(:uid as uuid)
                  {org_team_join("g", "parent", "cast(:uid as uuid)")}
                 where g.{key} = :v
                """
            ),
            {"v": value, "uid": str(user["id"]) if user else None},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Group not found")
    g = dict(row)
    g["id"] = str(g["id"])
    g["parent_id"] = str(g["parent_id"]) if g["parent_id"] else None
    g["organiser_id"] = str(g["organiser_id"]) if g["organiser_id"] else None
    g["category_ids"] = [str(c) for c in g.get("category_ids") or []]
    return g


async def role_of(db: AsyncSession, group_id: str, user: Optional[dict]) -> Optional[str]:
    if not user:
        return None
    return (
        await db.execute(
            text("select role from public.group_members where group_id = :g and user_id = :u"),
            {"g": group_id, "u": str(user["id"])},
        )
    ).scalar_one_or_none()


async def card(db: AsyncSession, group_id: str, user: Optional[dict]) -> dict[str, Any]:
    row = (
        await db.execute(
            text(
                """
                with page as (
                  select g.*, mr.role as my_role
                    from public.groups g
                    left join public.group_members mr
                      on mr.group_id = g.id and mr.user_id = cast(:uid as uuid)
                   where g.id = :id
                )
                """
                + CARD_SELECT
            ),
            {"id": group_id, "uid": str(user["id"]) if user else None},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Group not found")
    return group_out(row)


async def directory(
    db: AsyncSession,
    user: Optional[dict],
    *,
    kind: Optional[str],
    mine: bool,
    category: Optional[str],
    university: Optional[str],
    q: Optional[str],
    limit: int,
    cursor: Optional[str],
) -> dict[str, Any]:
    """Groups I'm in first, then by name. Keyset on (not joined, lower(name), id)."""
    uid = str(user["id"]) if user else None
    where = ["true"]
    params: dict[str, Any] = {"uid": uid}

    if mine or kind == "circle":
        # Circles are never browsable: listing them always means mine.
        if not uid:
            return {"items": [], "total": 0, "next_cursor": None, "page_size": limit}
        where.append("mr.role is not null")
    if kind:
        where.append("g.kind = :kind")
        params["kind"] = kind
    if not mine and kind != "circle":
        where.append("g.kind = 'community'")
        if not is_orbit_admin(user):
            where.append("(g.visibility in ('public', 'approval') or mr.role is not null)")
    if mine and not kind:
        # Channels are listed under their community; this list stays what older clients expect.
        where.append("g.kind <> 'channel'")
    if category:
        try:
            params["category_id"] = str(UUID(category))
            where.append("g.category_ids @> array[cast(:category_id as uuid)]")
        except ValueError:
            params["category_slug"] = category.strip().lower()
            where.append(
                "g.category_ids @> array[(select id from public.categories"
                " where slug = :category_slug)]"
            )
    if university:
        where.append("lower(g.university) = lower(:university)")
        params["university"] = university.strip()
    if q and q.strip():
        where.append(
            "(g.name ilike :q escape '\\' or coalesce(g.description, '') ilike :q escape '\\')"
        )
        params["q"] = f"%{escape_like(q.strip())}%"

    base_from = f"""
        from public.groups g
        left join public.group_members mr on mr.group_id = g.id and mr.user_id = cast(:uid as uuid)
       where {" and ".join(where)}
    """
    total = (await db.execute(text("select count(*) " + base_from), params)).scalar() or 0

    page_where = ""
    after = decode_cursor(cursor, "bsu")
    if after:
        page_where = (
            " and ((mr.role is null), lower(g.name), g.id)"
            " > (cast(:c_unjoined as boolean), cast(:c_name as text), cast(:c_id as uuid))"
        )
        params.update(c_unjoined=after[0], c_name=after[1], c_id=after[2])
    rows = (
        await db.execute(
            text(
                f"""
                with page as (
                  select g.*, mr.role as my_role
                  {base_from}{page_where}
                   order by (mr.role is null), lower(g.name), g.id
                   limit :limit
                )
                {CARD_SELECT}
                order by (p.my_role is null), lower(p.name), p.id
                """
            ),
            {**params, "limit": limit + 1},
        )
    ).mappings().all()
    more = len(rows) > limit
    rows = rows[:limit]
    items = [group_out(r) for r in rows]
    # Nobody sees faces in a group everyone was put in, unless they run it.
    previews = await member_previews(
        db,
        [
            i["id"]
            for i in items
            if not i["auto_join"] or i["my_role"] == "admin" or is_orbit_admin(user)
        ],
    )
    for item in items:
        item["members_preview"] = previews.get(item["id"], [])
    next_cursor = None
    if more and items:
        last = items[-1]
        next_cursor = encode_cursor(last["my_role"] is None, last["name"].lower(), last["id"])
    return {"items": items, "total": int(total), "next_cursor": next_cursor, "page_size": limit}


async def channels(
    db: AsyncSession,
    parent: dict,
    user: Optional[dict],
    *,
    category: Optional[str],
    q: Optional[str],
    limit: int,
    cursor: Optional[str],
    manager: bool,
) -> dict[str, Any]:
    """The channels of a community I may see: Announcements first, then those I'm in, then by
    name. Keyset on (not announcements, not joined, lower(name), id). Private ones only for
    their members and for `manager`. Two queries (page, total)."""
    where = ["g.parent_id = :parent"]
    params: dict[str, Any] = {"parent": parent["id"], "uid": str(user["id"]) if user else None}
    if not manager:
        where.append("(g.visibility in ('public', 'approval') or mr.role is not null)")
    if category:
        try:
            params["category_id"] = str(UUID(category))
            where.append("g.category_ids @> array[cast(:category_id as uuid)]")
        except ValueError:
            params["category_slug"] = category.strip().lower()
            where.append(
                "g.category_ids @> array[(select id from public.categories"
                " where slug = :category_slug)]"
            )
    if q and q.strip():
        where.append(
            "(g.name ilike :q escape '\\' or coalesce(g.description, '') ilike :q escape '\\')"
        )
        params["q"] = f"%{escape_like(q.strip())}%"
    base_from = f"""
        from public.groups g
        left join public.group_members mr on mr.group_id = g.id and mr.user_id = cast(:uid as uuid)
       where {" and ".join(where)}
    """
    total = (await db.execute(text("select count(*) " + base_from), params)).scalar() or 0

    page_where = ""
    after = decode_cursor(cursor, "bbsu")
    if after:
        page_where = (
            " and ((not g.is_announcements), (mr.role is null), lower(g.name), g.id)"
            " > (cast(:c_other as boolean), cast(:c_unjoined as boolean),"
            " cast(:c_name as text), cast(:c_id as uuid))"
        )
        params.update(c_other=after[0], c_unjoined=after[1], c_name=after[2], c_id=after[3])
    rows = (
        await db.execute(
            text(
                f"""
                with page as (
                  select g.*, mr.role as my_role
                  {base_from}{page_where}
                   order by (not g.is_announcements), (mr.role is null), lower(g.name), g.id
                   limit :limit
                )
                {CARD_SELECT}
                order by (not p.is_announcements), (p.my_role is null), lower(p.name), p.id
                """
            ),
            {**params, "limit": limit + 1},
        )
    ).mappings().all()
    more = len(rows) > limit
    items = [group_out(r) for r in rows[:limit]]
    next_cursor = None
    if more and items:
        last = items[-1]
        next_cursor = encode_cursor(
            not last["is_announcements"], last["my_role"] is None, last["name"].lower(), last["id"]
        )
    return {"items": items, "total": int(total), "next_cursor": next_cursor, "page_size": limit}


async def resolve_category(db: AsyncSession, ref: Optional[str]) -> Optional[str]:
    """A category id from an id or a slug; None for blank. 422 when unknown."""
    if not ref or not ref.strip():
        return None
    return (await resolve_categories(db, [ref]))[0]


async def resolve_categories(db: AsyncSession, refs: list[str]) -> list[str]:
    """Category ids from ids or slugs, in the order given, without repeats. One query;
    422 naming any that don't exist."""
    wanted = [r.strip() for r in refs if r and r.strip()]
    if not wanted:
        return []
    ids, slugs = [], []
    for ref in wanted:
        try:
            ids.append(str(UUID(ref)))
        except ValueError:
            slugs.append(ref.lower())
    rows = (
        await db.execute(
            text(
                "select id, slug from public.categories"
                " where id = any(cast(:ids as uuid[])) or slug = any(cast(:slugs as text[]))"
            ),
            {"ids": ids, "slugs": slugs},
        )
    ).all()
    by_ref = {str(r[0]): str(r[0]) for r in rows} | {r[1]: str(r[0]) for r in rows}
    unknown = [ref for ref in wanted if by_ref.get(ref) is None and by_ref.get(ref.lower()) is None]
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"Unknown category: {', '.join(unknown)}"
        )
    out: list[str] = []
    for ref in wanted:
        cid = by_ref.get(ref) or by_ref[ref.lower()]
        if cid not in out:
            out.append(cid)
    return out


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:50].strip("-") or "community"


# Slugs the frontend's /communities routes use for pages of their own.
RESERVED_SLUGS = {"new"}


async def unique_slug(db: AsyncSession, base: str, exclude_id: Optional[str] = None) -> str:
    taken = RESERVED_SLUGS | set(
        (
            await db.execute(
                text(
                    "select slug from public.groups where (slug = :b or slug like :p)"
                    " and id is distinct from cast(:x as uuid)"
                ),
                {"b": base, "p": f"{base}-%", "x": exclude_id},
            )
        ).scalars()
    )
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


# ─── access ─────────────────────────────────────────────────────────────────


def can_manage(g: dict, role: Optional[str], user: Optional[dict]) -> bool:
    """`g` comes from `resolve(db, ref, user)`, for a channel's `parent_role` and the
    organisation team's `org_role`."""
    if not user:
        return False
    if role == "admin":
        return True
    if g["kind"] == "circle":
        return False
    return (
        is_orbit_admin(user)
        or g.get("org_role") is not None
        or (g["kind"] == "channel" and g.get("parent_role") == "admin")
    )


def can_see_card(g: dict, role: Optional[str], user: Optional[dict]) -> bool:
    if role or can_manage(g, role, user):
        return True
    if g["kind"] == "channel":
        if g["visibility"] not in LISTED:
            return False
        return g["parent_visibility"] in LISTED or g.get("parent_role") is not None
    return g["kind"] == "community" and g["visibility"] in LISTED


def can_read_content(g: dict, role: Optional[str], user: Optional[dict]) -> bool:
    """The event feed: members; a public community: anyone; Orbit admins: communities; a
    public channel: whoever may see its card; a channel's managers."""
    if role:
        return True
    if g["kind"] == "circle":
        return False
    if g["kind"] == "channel":
        return can_manage(g, role, user) or (g["visibility"] == "public" and can_see_card(g, role, user))
    return g["visibility"] == "public" or is_orbit_admin(user)


def can_see_members(g: dict, role: Optional[str], user: Optional[dict]) -> bool:
    """Who is in it. An `auto_join` group is everyone, not people who chose each other:
    only those who can manage it see its list; the rest get the count."""
    if can_manage(g, role, user):
        return True
    if g["auto_join"]:
        return False
    if g["kind"] == "channel":
        return role is not None
    return can_read_content(g, role, user)


def can_invite(g: dict, role: Optional[str], user: Optional[dict]) -> bool:
    if g["kind"] == "circle":
        return role is not None
    return can_manage(g, role, user)


def can_share(g: dict, role: Optional[str], user: Optional[dict]) -> bool:
    if g["kind"] == "circle":
        return role is not None
    if can_manage(g, role, user):
        return True
    if g["kind"] == "channel" and g["post_policy"] == "moderators":
        return False
    return role is not None and bool(g["allow_member_shares"])


def deny(user: Optional[dict], message: str) -> HTTPException:
    if not user:
        return HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in to continue")
    return HTTPException(status.HTTP_403_FORBIDDEN, message)


def deny_read(g: dict, role: Optional[str], user: Optional[dict], message: str) -> HTTPException:
    """`deny`, except a channel I can't see isn't there at all, so its existence isn't confirmed."""
    if g["kind"] == "channel" and not can_see_card(g, role, user):
        return HTTPException(status.HTTP_404_NOT_FOUND, "Group not found")
    return deny(user, message)


async def admin_count(db: AsyncSession, group_id: str) -> int:
    return int(
        (
            await db.execute(
                text(
                    "select count(*) from public.group_members"
                    " where group_id = :g and role = 'admin'"
                ),
                {"g": group_id},
            )
        ).scalar()
        or 0
    )


async def remove_membership(db: AsyncSession, group_id: str, user_id: str) -> None:
    """Someone leaves or is removed: from the group and, for a community, from all its
    channels. Their interest marks and chat read state go too. A fixed three statements."""
    params = {"g": group_id, "u": user_id}
    await db.execute(
        text(
            "delete from public.group_members where user_id = :u and group_id in"
            " (select id from public.groups where id = :g or parent_id = :g)"
        ),
        params,
    )
    await db.execute(
        text(
            "delete from public.group_event_interests where user_id = :u and group_id in"
            " (select id from public.groups where id = :g or parent_id = :g)"
        ),
        params,
    )
    await db.execute(
        text(
            "delete from public.conversation_members cm using public.conversations c"
            " where c.id = cm.conversation_id and cm.user_id = :u and c.group_id in"
            " (select id from public.groups where id = :g or parent_id = :g)"
        ),
        params,
    )


# ─── members ────────────────────────────────────────────────────────────────


async def members(db: AsyncSession, group_id: str, limit: int = 500) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            text(
                f"""
                select m.role, m.created_at, {user_columns("m.user_id", "p", "u_")}
                  from public.group_members m
                  left join public.profiles p on p.id = m.user_id
                 where m.group_id = :g
                 order by (m.role = 'admin') desc, p.full_name nulls last, m.created_at
                 limit :limit
                """
            ),
            {"g": group_id, "limit": limit},
        )
    ).mappings().all()
    return [
        {"user": user_brief(r, "u_"), "role": r["role"], "joined_at": r["created_at"]}
        for r in rows
    ]


async def member(db: AsyncSession, group_id: str, user_id: str) -> Optional[dict[str, Any]]:
    """One member, in the same shape as `members`."""
    row = (
        await db.execute(
            text(
                f"""
                select m.role, m.created_at, {user_columns("m.user_id", "p", "u_")}
                  from public.group_members m
                  left join public.profiles p on p.id = m.user_id
                 where m.group_id = :g and m.user_id = :u
                """
            ),
            {"g": group_id, "u": user_id},
        )
    ).mappings().first()
    if not row:
        return None
    return {"user": user_brief(row, "u_"), "role": row["role"], "joined_at": row["created_at"]}


async def member_total(db: AsyncSession, group_id: str) -> int:
    return int(
        (
            await db.execute(
                text("select count(*) from public.group_members where group_id = :g"),
                {"g": group_id},
            )
        ).scalar()
        or 0
    )


async def member_previews(db: AsyncSession, group_ids: list[str], per_group: int = 5) -> dict[str, list]:
    """The first few members of each group (admins first), for avatar stacks. One query."""
    if not group_ids:
        return {}
    rows = (
        await db.execute(
            text(
                f"""
                select g.id as group_id, {user_columns("m.user_id", "p", "u_")}
                  from unnest(cast(:ids as uuid[])) as g(id)
                  cross join lateral (
                    select m.user_id from public.group_members m
                     where m.group_id = g.id
                     order by (m.role = 'admin') desc, m.created_at
                     limit :per
                  ) m
                  left join public.profiles p on p.id = m.user_id
                """
            ),
            {"ids": group_ids, "per": per_group},
        )
    ).mappings().all()
    out: dict[str, list] = {}
    for r in rows:
        out.setdefault(str(r["group_id"]), []).append(user_brief(r, "u_"))
    return out


# ─── the event feed ─────────────────────────────────────────────────────────


def _feed_branches(g: dict, when: str, keyset: str, *, limited: bool = True) -> str:
    """Shared events, plus (communities, upcoming only) what its categories pick up.

    Each branch is ordered and limited on its own, and the matched branch reads
    each category separately (a lateral per category on `events_feed_idx`), so
    every read starts at the cursor position; the outer query merges at most
    (1 + categories) x limit rows.
    """
    timing = UPCOMING if when == "upcoming" else f"not ({UPCOMING})"
    order = "e.starts_at, e.id" if when == "upcoming" else "e.starts_at desc, e.id desc"
    page = f"order by {order} limit :limit" if limited else ""
    shared = f"""
        (select ge.event_id, ge.shared_by, ge.note, ge.created_at as shared_at,
                e.starts_at, e.id as sort_id
           from public.group_events ge
           join public.events e on e.id = ge.event_id
           left join public.organisers o on o.id = e.organiser_id
          where ge.group_id = :g
            and e.status in ('published', 'cancelled', 'expired') and {EVENT_VISIBLE}
            and {timing}{keyset}
          {page})
    """
    if not (g["kind"] != "circle" and g.get("category_ids") and when == "upcoming"):
        return shared
    matched = f"""
        (select m.id, null::uuid, null::text, null::timestamptz, m.starts_at, m.id
           from (select distinct unnest(cast(:categories as uuid[]))) as cat(id)
           cross join lateral (
             select e.id, e.starts_at
               from public.events e
               left join public.organisers o on o.id = e.organiser_id
              where e.category_id = cat.id and {MATCHED_REST}
                and {EVENT_VISIBLE} and {timing}{keyset}
                and not exists (select 1 from public.group_events ge
                                 where ge.group_id = :g and ge.event_id = e.id)
              {page}
           ) m)
    """
    return shared + " union all " + matched


async def feed(
    db: AsyncSession,
    g: dict,
    user: Optional[dict],
    *,
    when: str,
    limit: int,
    cursor: Optional[str],
    show_people: bool,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "g": g["id"],
        "limit": limit + 1,
        "categories": g.get("category_ids") or [],
        "university": g.get("university") or None,
    }
    keyset = ""
    after = decode_cursor(cursor, "tu")
    if after:
        op = ">" if when == "upcoming" else "<"
        keyset = f" and (e.starts_at, e.id) {op} (:c_start, cast(:c_id as uuid))"
        params.update(c_start=after[0], c_id=after[1])
    order = "starts_at, sort_id" if when == "upcoming" else "starts_at desc, sort_id desc"
    rows = (
        await db.execute(
            text(
                f"""
                select f.event_id, f.note, f.shared_at, f.starts_at,
                       {user_columns("f.shared_by", "sp", "s_")}
                  from ({_feed_branches(g, when, keyset)}) f
                  left join public.profiles sp on sp.id = f.shared_by
                 order by {order}
                 limit :limit
                """
            ),
            params,
        )
    ).mappings().all()
    more = len(rows) > limit
    rows = rows[:limit]

    # The total only comes with the first page (later pages keep the first answer).
    total = None
    if not after:
        total = (
            await db.execute(
                text(f"select count(*) from ({_feed_branches(g, when, '', limited=False)}) f"),
                {k: v for k, v in params.items() if k != "limit"},
            )
        ).scalar() or 0

    ids = [str(r["event_id"]) for r in rows]
    cards: dict[str, dict] = {}
    if ids:
        found = (await db.execute(card_query().where(Event.id.in_(ids)))).all()
        for item in await with_counts(db, [event_card(*row) for row in found]):
            cards[str(item["id"])] = item
    people = await _interest(db, g["id"], ids, str(user["id"]) if user else None) if ids else {}

    items = []
    for r in rows:
        eid = str(r["event_id"])
        if eid not in cards:
            continue
        marks = people.get(eid, {})
        items.append(
            {
                "event": cards[eid],
                "shared_by": user_brief(r, "s_"),
                "note": r["note"],
                "shared_at": r["shared_at"],
                "interested": marks.get("interested", []) if show_people else [],
                "going": marks.get("going", []) if show_people else [],
                "interested_count": marks.get("interested_count", 0),
                "going_count": marks.get("going_count", 0),
                "my_status": marks.get("mine"),
            }
        )
    next_cursor = None
    if more and rows:
        last = rows[-1]
        next_cursor = encode_cursor(last["starts_at"], last["event_id"])
    return {"items": items, "total": total, "next_cursor": next_cursor, "page_size": limit}


async def _interest(
    db: AsyncSession, group_id: str, event_ids: list[str], uid: Optional[str]
) -> dict[str, dict]:
    """Per event: up to 8 interested and 8 going members (oldest first), the full counts,
    and my own status. One query; only current members count."""
    rows = (
        await db.execute(
            text(
                f"""
                select x.event_id, x.status, x.n, x.rn, x.is_me,
                       {user_columns("x.user_id", "p", "u_")}
                  from (
                    select i.event_id, i.status, i.user_id,
                           i.user_id = cast(:uid as uuid) as is_me,
                           count(*) over (partition by i.event_id, i.status) as n,
                           row_number() over (
                             partition by i.event_id, i.status order by i.created_at, i.user_id
                           ) as rn
                      from public.group_event_interests i
                      join public.group_members m
                        on m.group_id = i.group_id and m.user_id = i.user_id
                     where i.group_id = :g and i.event_id = any(cast(:ids as uuid[]))
                  ) x
                  left join public.profiles p on p.id = x.user_id
                 where x.rn <= :cap or x.is_me
                 order by x.event_id, x.status, x.rn
                """
            ),
            {"g": group_id, "ids": event_ids, "uid": uid, "cap": AVATARS_PER_EVENT},
        )
    ).mappings().all()
    out: dict[str, dict] = {}
    for r in rows:
        entry = out.setdefault(
            str(r["event_id"]),
            {"interested": [], "going": [], "interested_count": 0, "going_count": 0, "mine": None},
        )
        entry[f"{r['status']}_count"] = int(r["n"])
        if r["is_me"]:
            entry["mine"] = r["status"]
        if r["rn"] <= AVATARS_PER_EVENT:
            entry[r["status"]].append(user_brief(r, "u_"))
    return out


async def in_feed(db: AsyncSession, g: dict, event_id: str) -> bool:
    """Whether an event is shared with this group or (a community) picked up by its category."""
    shared = (
        await db.execute(
            text("select 1 from public.group_events where group_id = :g and event_id = :e"),
            {"g": g["id"], "e": event_id},
        )
    ).first()
    if shared:
        return True
    if g["kind"] == "circle" or not g.get("category_ids"):
        return False
    return (
        await db.execute(
            text(f"select 1 from public.events e where e.id = :e and {MATCHED}"),
            {"e": event_id, "categories": g["category_ids"], "university": g.get("university")},
        )
    ).first() is not None
