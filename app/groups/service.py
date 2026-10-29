"""Circles & Communities: lookups, access rules and the shapes the API returns.

Who may do what (the router enforces it through these helpers):

| | Circle | Community |
|---|---|---|
| see the group card | members (and people it invited) | anyone if public/approval; members if private |
| see members, events | members | members; public: anyone |
| read announcements | members | members (and Orbit admins) |
| manage (edit, invites list, roles, delete) | group admins | group admins and Orbit admins |
| invite by email / mint a link | any member | group admins and Orbit admins |
| share an event | any member | members if `allow_member_shares`, else admins |

Orbit admins get no special access to Circles: they are private friend groups.

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
SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
AVATARS_PER_EVENT = 8

# An event anyone may open (the events_select rule) over `events e left join organisers o`.
EVENT_VISIBLE = "(e.organiser_id is null or o.status = 'approved')"
UPCOMING = "coalesce(e.ends_at, e.starts_at) >= now()"

# Events a community picks up by itself: published, not a duplicate, its category and
# (when set) its university. Bound as :category / :university from the group row.
MATCHED = """
    e.category_id = cast(:category as uuid)
    and e.status = 'published' and e.duplicate_of is null
    and (cast(:university as text) is null or lower(e.university) = lower(cast(:university as text)))
"""

# The card's computed columns, for rows of `p` (a page of groups that already carries
# `my_role`). Each lateral runs once per row of the page, on an index.
CARD_SELECT = f"""
select p.id, p.kind, p.name, p.slug, p.description, p.cover_url, p.university,
       p.visibility, p.category_id, c.name as category_name, c.slug as category_slug,
       p.allow_member_shares, p.created_by, p.created_at, p.my_role,
       mc.n as member_count,
       shared.n + matched.n as upcoming_event_count,
       pr.pending as pending_request
  from page p
  left join public.categories c on c.id = p.category_id
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
     where p.kind = 'community' and e.category_id = p.category_id
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
"""


def is_orbit_admin(user: Optional[dict]) -> bool:
    return bool(user) and user["role"] in ORBIT_ADMIN_ROLES


def group_out(row: Any) -> dict[str, Any]:
    """The group card every endpoint returns."""
    r = dict(row)
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
    }


def group_path(g: dict) -> str:
    """Where the frontend shows a group."""
    if g["kind"] == "circle":
        return f"/circles/{g['id']}"
    return f"/communities/{g.get('slug') or g['id']}"


def as_uuid(value: Any, what: str = "id") -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Not a valid {what}") from None


# ─── lookups ────────────────────────────────────────────────────────────────


async def resolve(db: AsyncSession, ref: str) -> dict:
    """A group by id, or by slug (communities). 404 when neither matches."""
    try:
        key, value = "id", str(UUID(ref))
    except ValueError:
        key, value = "slug", ref.strip().lower()
    row = (
        await db.execute(text(f"select * from public.groups where {key} = :v"), {"v": value})
    ).mappings().first()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Group not found")
    g = dict(row)
    g["id"] = str(g["id"])
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
    if category:
        try:
            params["category_id"] = str(UUID(category))
            where.append("g.category_id = cast(:category_id as uuid)")
        except ValueError:
            params["category_slug"] = category.strip().lower()
            where.append(
                "g.category_id = (select id from public.categories where slug = :category_slug)"
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
    previews = await member_previews(db, [i["id"] for i in items])
    for item in items:
        item["members_preview"] = previews.get(item["id"], [])
    next_cursor = None
    if more and items:
        last = items[-1]
        next_cursor = encode_cursor(last["my_role"] is None, last["name"].lower(), last["id"])
    return {"items": items, "total": int(total), "next_cursor": next_cursor, "page_size": limit}


async def resolve_category(db: AsyncSession, ref: Optional[str]) -> Optional[str]:
    """A category id from an id or a slug; None for blank. 422 when unknown."""
    if not ref or not ref.strip():
        return None
    ref = ref.strip()
    try:
        key, value = "id", str(UUID(ref))
    except ValueError:
        key, value = "slug", ref.lower()
    found = (
        await db.execute(text(f"select id from public.categories where {key} = :v"), {"v": value})
    ).scalar_one_or_none()
    if not found:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unknown category")
    return str(found)


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:50].strip("-") or "community"


async def unique_slug(db: AsyncSession, base: str, exclude_id: Optional[str] = None) -> str:
    taken = set(
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
    if not user:
        return False
    return role == "admin" or (g["kind"] == "community" and is_orbit_admin(user))


def can_read_content(g: dict, role: Optional[str], user: Optional[dict]) -> bool:
    """Members and the event feed: members; a public community: anyone; Orbit admins: communities."""
    if role:
        return True
    if g["kind"] != "community":
        return False
    return g["visibility"] == "public" or is_orbit_admin(user)


def can_invite(g: dict, role: Optional[str], user: Optional[dict]) -> bool:
    if g["kind"] == "circle":
        return role is not None
    return can_manage(g, role, user)


def can_share(g: dict, role: Optional[str], user: Optional[dict]) -> bool:
    if g["kind"] == "circle":
        return role is not None
    if can_manage(g, role, user):
        return True
    return role is not None and bool(g["allow_member_shares"])


def deny(user: Optional[dict], message: str) -> HTTPException:
    if not user:
        return HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in to continue")
    return HTTPException(status.HTTP_403_FORBIDDEN, message)


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


async def drop_membership_traces(db: AsyncSession, group_id: str, user_id: str) -> None:
    """After someone leaves or is removed: their interest marks and chat read state go too."""
    params = {"g": group_id, "u": user_id}
    await db.execute(
        text("delete from public.group_event_interests where group_id = :g and user_id = :u"),
        params,
    )
    await db.execute(
        text(
            "delete from public.conversation_members cm using public.conversations c"
            " where c.id = cm.conversation_id and c.group_id = :g and cm.user_id = :u"
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
    """Shared events, plus (communities, upcoming only) what its category picks up.

    Each branch is ordered and limited on its own, so each reads its index from
    the cursor position; the outer query merges at most 2 x limit rows.
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
    if not (g["kind"] == "community" and g.get("category_id") and when == "upcoming"):
        return shared
    matched = f"""
        (select e.id, null::uuid, null::text, null::timestamptz, e.starts_at, e.id
           from public.events e
           left join public.organisers o on o.id = e.organiser_id
          where {MATCHED} and {EVENT_VISIBLE} and {timing}{keyset}
            and not exists (select 1 from public.group_events ge
                             where ge.group_id = :g and ge.event_id = e.id)
          {page})
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
        "category": str(g["category_id"]) if g.get("category_id") else None,
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
    if g["kind"] != "community" or not g.get("category_id"):
        return False
    return (
        await db.execute(
            text(f"select 1 from public.events e where e.id = :e and {MATCHED}"),
            {"e": event_id, "category": str(g["category_id"]), "university": g.get("university")},
        )
    ).first() is not None
