"""Circle and channel chat, and direct messages: access, the inbox queries, sending.

Access is decided live on every request:

* circle or channel chat — the caller must be in `group_members` for that group now;
* direct — the caller is one of the pair and neither has blocked the other;
  starting one or sending needs a group in common (no cold messages). An `auto_join`
  group doesn't count: being put in the same group as everyone isn't knowing them;
* posting in a channel with `post_policy='moderators'` — only those who can manage it.

The conversation list and the unread badge are circles and direct messages only. A
channel's unread count is on the channel list (`channel_activity`), so daily auto-posts
don't inflate the badge.

The two polling paths stay cheap: `unread_total` is one aggregate over the
caller's conversations (each count an index range on messages), and a thread
poll (`after=`) is one access query plus one index range scan, plus one batch per
kind of content (attachments, links, event cards) only when a message carries it.
"""

import logging
from functools import partial
from typing import Any, Optional
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import decode_cursor, encode_cursor
from app.events.models import Event
from app.events.service import card as event_card
from app.events.service import card_query, escape_like, with_counts
from app.groups.service import EVENT_VISIBLE, ORBIT_ADMIN_ROLES, group_path
from app.messaging import attachments as files
from app.messaging import links as message_links
from app.messaging.schemas import AttachmentIn
from app.notifications import inbox
from app.notifications.channels import channels_available, delete_subscriptions, send_push
from app.notifications.messages import direct_message_push
from app.notifications.service import _after_commit, channels_for, load_recipients
from app.organisers.models import Organiser
from app.uploads.release import release_images
from app.users.brief import user_brief, user_columns

logger = logging.getLogger(__name__)

MESSAGES_PER_MINUTE = 20  # sends and edits together
POST_REFUSED = "Only moderators can send messages here"
# A poll re-sends changes from slightly before `changed_since`, so an edit whose
# transaction commits just after a poll still reaches the next one. Clients apply
# `changed` by id, so a repeat is harmless.
CHANGE_OVERLAP_SECONDS = 5
MAX_CHANGES_PER_POLL = 200
DM_PUSH_QUIET_MINUTES = 10

# Messages from someone I blocked are hidden from me everywhere.
NOT_BLOCKED_SENDER = """
    not exists (select 1 from public.user_blocks b
                 where b.blocker_id = cast(:me as uuid) and b.blocked_id = m.sender_id)
"""

# What a message says in a list: its text, or what it carries. Over `messages m` and a
# `left join public.events e on e.id = m.event_id`.
PREVIEW = """
    coalesce(nullif(m.body, ''),
             case m.kind when 'event' then e.title when 'media' then 'Attachment' end)
"""

# Every conversation I can open, with the moment after which a message counts as unread
# (`since`: my last read, and for a group not before I joined) and its last activity.
# Circles and direct messages; a channel only when asked for by id (`:only`).
MINE = """
mine as (
  select c.id, c.kind, c.group_id, null::uuid as other_id,
         greatest(coalesce(cm.last_read_at, '-infinity'::timestamptz), gm.created_at) as since,
         coalesce(c.last_message_at, c.created_at) as activity
    from public.group_members gm
    join public.conversations c on c.group_id = gm.group_id
     and (c.kind = 'circle' or c.id = cast(:only as uuid))
    left join public.conversation_members cm
      on cm.conversation_id = c.id and cm.user_id = cast(:me as uuid)
   where gm.user_id = cast(:me as uuid)
  union all
  select d.id, d.kind, null::uuid, d.other_id,
         coalesce(cm.last_read_at, '-infinity'::timestamptz),
         coalesce(d.last_message_at, d.created_at)
    from (
      select c.id, c.kind, c.last_message_at, c.created_at,
             case when c.user_a = cast(:me as uuid) then c.user_b else c.user_a end as other_id
        from public.conversations c
       where c.kind = 'direct'
         and (c.user_a = cast(:me as uuid) or c.user_b = cast(:me as uuid))
         and (c.last_message_at is not null or c.created_by = cast(:me as uuid))
    ) d
    left join public.conversation_members cm
      on cm.conversation_id = d.id and cm.user_id = cast(:me as uuid)
   where not exists (
     select 1 from public.user_blocks b
      where (b.blocker_id = cast(:me as uuid) and b.blocked_id = d.other_id)
         or (b.blocker_id = d.other_id and b.blocked_id = cast(:me as uuid))
   )
)
"""

UNREAD_LATERAL = f"""
cross join lateral (
  select count(*) as n
    from public.messages m
   where m.conversation_id = mine.id and m.created_at > mine.since
     and m.deleted_at is null and m.sender_id is distinct from cast(:me as uuid)
     and {NOT_BLOCKED_SENDER}
) u
"""


def message_out(r: Any, conversation_id: Optional[str] = None) -> dict[str, Any]:
    """A message without what it carries; `messages_out` fills that in."""
    deleted = r["deleted_at"] is not None
    return {
        "id": str(r["id"]),
        "conversation_id": conversation_id or str(r["conversation_id"]),
        "sender": user_brief(r, "s_"),
        "body": "" if deleted else r["body"],
        "deleted": deleted,
        "created_at": r["created_at"],
        "edited_at": r.get("edited_at"),
        "kind": r["kind"],
        "event": None,
        "attachments": [],
        "links": [],
    }


async def _event_cards(db: AsyncSession, event_ids: list[str]) -> dict[str, dict]:
    """Event cards by id, in one batch. Only events anyone may still open: an event that
    was unpublished or hidden since shows as no card."""
    if not event_ids:
        return {}
    found = (
        await db.execute(
            card_query().where(
                Event.id.in_(event_ids),
                Event.status.in_(("published", "cancelled", "expired")),
                (Event.organiser_id.is_(None)) | (Organiser.status == "approved"),
            )
        )
    ).all()
    return {str(c["id"]): c for c in await with_counts(db, [event_card(*row) for row in found])}


async def messages_out(
    db: AsyncSession, rows: list[Any], conversation_id: str
) -> list[dict[str, Any]]:
    """Messages with their attachments, links and event cards, a batch for each that any of
    them needs, never a query per message. A deleted message carries nothing."""
    out = [message_out(r, conversation_id) for r in rows]
    live = [r for r in rows if r["deleted_at"] is None]
    by_id = {m["id"]: m for m in out}
    media = [str(r["id"]) for r in live if r["kind"] == "media"]
    linked = [str(r["id"]) for r in live if "://" in r["body"]]
    if media:
        found = (
            await db.execute(
                text(
                    "select * from public.message_attachments where message_id = any(cast(:ids as uuid[]))"
                    " order by created_at, id"
                ),
                {"ids": media},
            )
        ).mappings().all()
        for a in found:
            by_id[str(a["message_id"])]["attachments"].append(files.attachment_out(a))
    if linked:
        found = (
            await db.execute(
                text(
                    "select * from public.message_links where message_id = any(cast(:ids as uuid[]))"
                ),
                {"ids": linked},
            )
        ).mappings().all()
        for link in found:
            by_id[str(link["message_id"])]["links"].append(message_links.link_out(link))
        bodies = {str(r["id"]): r["body"] for r in live}
        for message_id in linked:
            by_id[message_id]["links"].sort(key=lambda link: bodies[message_id].find(link["url"]))
    events = [r for r in live if r["kind"] == "event" and r["event_id"]]
    if events:
        cards = await _event_cards(db, list({str(r["event_id"]) for r in events}))
        for r in events:
            by_id[str(r["id"])]["event"] = cards.get(str(r["event_id"]))
    return out


def _group_brief(r: Any) -> Optional[dict[str, Any]]:
    if not r["group_id"]:
        return None
    return {
        "id": str(r["group_id"]),
        "name": r["group_name"],
        "kind": r["group_kind"],
        "slug": r["group_slug"],
        "parent_slug": r["parent_slug"],
        "parent_name": r["parent_name"],
        "post_policy": r["post_policy"],
        "is_announcements": r["is_announcements"],
    }


def _item(r: Any) -> dict[str, Any]:
    last = None
    if r["lm_at"] is not None:
        last = {
            "body": r["lm_body"],
            "kind": r["lm_kind"],
            "preview": r["lm_preview"],
            "sender_id": str(r["lm_sender"]) if r["lm_sender"] else None,
            "created_at": r["lm_at"],
        }
    return {
        "id": str(r["id"]),
        "kind": r["kind"],
        "group": _group_brief(r),
        "other_user": user_brief(r, "o_") if r["kind"] == "direct" else None,
        "last_message": last,
        "unread_count": int(r["unread"] or 0),
        "last_activity_at": r["activity"],
    }


# ─── the conversation list and the badge ────────────────────────────────────


async def unread_total(db: AsyncSession, me: str) -> int:
    """One aggregate query: unread messages across my circles and direct conversations."""
    return int(
        (
            await db.execute(
                text(f"with {MINE} select coalesce(sum(u.n), 0) from mine {UNREAD_LATERAL}"),
                {"me": me, "only": None},
            )
        ).scalar()
        or 0
    )


NO_ACTIVITY = {"unread_count": 0, "last_message_at": None, "last_message_preview": None}


async def channel_activity(
    db: AsyncSession, me: Optional[str], group_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """Per channel: last activity, and for the ones I'm in, unread count and a preview of the
    last message (the chat is for members). One query, an index range per channel."""
    if not group_ids:
        return {}
    rows = (
        await db.execute(
            text(
                f"""
                select c.group_id, c.last_message_at, lm.preview, u.n as unread
                  from public.conversations c
                  left join public.group_members gm
                    on gm.group_id = c.group_id and gm.user_id = cast(:me as uuid)
                  left join public.conversation_members cm
                    on cm.conversation_id = c.id and cm.user_id = cast(:me as uuid)
                  left join lateral (
                    select left({PREVIEW}, 200) as preview
                      from public.messages m
                      left join public.events e on e.id = m.event_id
                     where m.conversation_id = c.id and m.deleted_at is null
                       and gm.user_id is not null and {NOT_BLOCKED_SENDER}
                     order by m.created_at desc, m.id desc
                     limit 1
                  ) lm on true
                  cross join lateral (
                    select count(*) as n
                      from public.messages m
                     where m.conversation_id = c.id and gm.user_id is not null
                       and m.created_at > greatest(coalesce(cm.last_read_at, '-infinity'::timestamptz),
                                                   gm.created_at)
                       and m.deleted_at is null and m.sender_id is distinct from cast(:me as uuid)
                       and {NOT_BLOCKED_SENDER}
                  ) u
                 where c.group_id = any(cast(:ids as uuid[]))
                """
            ),
            {"me": me, "ids": group_ids},
        )
    ).mappings().all()
    return {
        str(r["group_id"]): {
            "unread_count": int(r["unread"] or 0),
            "last_message_at": r["last_message_at"],
            "last_message_preview": r["preview"],
        }
        for r in rows
    }


async def conversations(
    db: AsyncSession, me: str, *, limit: int, cursor: Optional[str], only: Optional[str] = None
) -> dict[str, Any]:
    """Most recent activity first; keyset on (activity, id). `only` reads one conversation,
    which may be a channel's."""
    params: dict[str, Any] = {"me": me, "limit": limit + 1, "only": only}
    where = ["true"]
    after = decode_cursor(cursor, "tu")
    if after:
        where.append("(mine.activity, mine.id) < (:c_at, cast(:c_id as uuid))")
        params.update(c_at=after[0], c_id=after[1])
    if only:
        where.append("mine.id = cast(:only as uuid)")
    rows = (
        await db.execute(
            text(
                f"""
                with {MINE}
                select mine.id, mine.kind, mine.group_id, mine.activity, g.name as group_name,
                       g.kind as group_kind, g.slug as group_slug, g.post_policy, g.is_announcements,
                       parent.slug as parent_slug, parent.name as parent_name,
                       {user_columns("mine.other_id", "op", "o_")},
                       lm.body as lm_body, lm.sender_id as lm_sender, lm.created_at as lm_at,
                       lm.kind as lm_kind, lm.preview as lm_preview,
                       u.n as unread
                  from mine
                  left join public.groups g on g.id = mine.group_id
                  left join public.groups parent on parent.id = g.parent_id
                  left join public.profiles op on op.id = mine.other_id
                  left join lateral (
                    select m.body, m.sender_id, m.created_at, m.kind, left({PREVIEW}, 200) as preview
                      from public.messages m
                      left join public.events e on e.id = m.event_id
                     where m.conversation_id = mine.id and m.deleted_at is null
                       and {NOT_BLOCKED_SENDER}
                     order by m.created_at desc, m.id desc
                     limit 1
                  ) lm on true
                  {UNREAD_LATERAL}
                 where {" and ".join(where)}
                 order by mine.activity desc, mine.id desc
                 limit :limit
                """
            ),
            params,
        )
    ).mappings().all()
    more = len(rows) > limit
    rows = rows[:limit]
    items = [_item(r) for r in rows]
    next_cursor = None
    if more and rows:
        next_cursor = encode_cursor(rows[-1]["activity"], rows[-1]["id"])
    return {"items": items, "next_cursor": next_cursor}


# ─── access ─────────────────────────────────────────────────────────────────


async def open_conversation(db: AsyncSession, conversation_id: UUID, me: str) -> dict[str, Any]:
    """The conversation if I may read it now; 404/403 otherwise. One query."""
    row = (
        await db.execute(
            text(
                f"""
                select c.id, c.kind, c.group_id, g.name as group_name, now() as polled_at,
                       g.kind as group_kind, g.slug as group_slug, g.post_policy, g.is_announcements,
                       g.auto_join, g.parent_id,
                       parent.slug as parent_slug, parent.name as parent_name,
                       gm.created_at as joined_at, gm.role as my_role, pm.role as parent_role,
                       mine.role as orbit_role,
                       (c.user_a = cast(:me as uuid) or c.user_b = cast(:me as uuid)) as participant,
                       {user_columns("x.other_id", "op", "o_")},
                       exists (
                         select 1 from public.user_blocks b
                          where (b.blocker_id = cast(:me as uuid) and b.blocked_id = x.other_id)
                             or (b.blocker_id = x.other_id and b.blocked_id = cast(:me as uuid))
                       ) as blocked
                  from public.conversations c
                  cross join lateral (
                    select case when c.kind <> 'direct' then null
                                when c.user_a = cast(:me as uuid) then c.user_b
                                else c.user_a end as other_id
                  ) x
                  left join public.groups g on g.id = c.group_id
                  left join public.groups parent on parent.id = g.parent_id
                  left join public.group_members gm
                    on gm.group_id = c.group_id and gm.user_id = cast(:me as uuid)
                  left join public.group_members pm
                    on pm.group_id = g.parent_id and pm.user_id = cast(:me as uuid)
                  left join public.profiles mine on mine.id = cast(:me as uuid)
                  left join public.profiles op on op.id = x.other_id
                 where c.id = :id
                """
            ),
            {"id": str(conversation_id), "me": me},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    if row["kind"] != "direct":
        if row["joined_at"] is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"You're not in this {row['kind']}")
    else:
        if not row["participant"]:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
        if row["blocked"]:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "You can't message this person")
    return dict(row)


def can_moderate(c: dict[str, Any]) -> bool:
    """A channel's managers (its admins, its community's admins, Orbit admins) may delete
    anyone's message and post whatever its `post_policy`. Circles and direct messages have none."""
    return c["kind"] == "channel" and (
        c["my_role"] == "admin" or c["parent_role"] == "admin" or c["orbit_role"] in ORBIT_ADMIN_ROLES
    )


def can_post(c: dict[str, Any]) -> bool:
    return c["kind"] != "channel" or c["post_policy"] != "moderators" or can_moderate(c)


def permissions(c: dict[str, Any]) -> dict[str, bool]:
    return {"can_post": can_post(c), "can_moderate": can_moderate(c)}


def slim(c: dict[str, Any]) -> dict[str, Any]:
    """The conversation fields a poll returns (no counts, no member list)."""
    return {
        "id": str(c["id"]),
        "kind": c["kind"],
        "group": _group_brief(c),
        "other_user": user_brief(c, "o_") if c["kind"] == "direct" else None,
        **permissions(c),
    }


async def share_a_group(db: AsyncSession, a: str, b: str) -> bool:
    """Who may message whom: a circle, community or channel in common that they chose. An
    `auto_join` group has everyone in it, so it says nothing about knowing someone."""
    return bool(
        (
            await db.execute(
                text(
                    "select exists (select 1 from public.group_members x"
                    " join public.group_members y on y.group_id = x.group_id"
                    " join public.groups g on g.id = x.group_id"
                    " where x.user_id = :a and y.user_id = :b and not g.auto_join)"
                ),
                {"a": a, "b": b},
            )
        ).scalar()
    )


async def blocked_either_way(db: AsyncSession, a: str, b: str) -> bool:
    return bool(
        (
            await db.execute(
                text(
                    "select exists (select 1 from public.user_blocks"
                    " where (blocker_id = :a and blocked_id = :b)"
                    " or (blocker_id = :b and blocked_id = :a))"
                ),
                {"a": a, "b": b},
            )
        ).scalar()
    )


async def members(db: AsyncSession, c: dict[str, Any], me: dict) -> list[dict[str, Any]]:
    if c["kind"] == "channel" and c["auto_join"] and not can_moderate(c):
        return []
    if c["kind"] != "direct":
        rows = (
            await db.execute(
                text(
                    f"""
                    select {user_columns("m.user_id", "p", "u_")}
                      from public.group_members m
                      left join public.profiles p on p.id = m.user_id
                     where m.group_id = :g
                     order by p.full_name nulls last, m.created_at
                     limit 200
                    """
                ),
                {"g": str(c["group_id"])},
            )
        ).mappings().all()
        return [user_brief(r, "u_") for r in rows]
    myself = {
        "id": str(me["id"]),
        "full_name": me.get("full_name"),
        "avatar_url": me.get("avatar_url"),
        "university": me.get("university"),
    }
    return [myself, user_brief(c, "o_")]


# ─── the thread ─────────────────────────────────────────────────────────────


MESSAGE_COLUMNS = f"""
    m.id, m.body, m.deleted_at, m.edited_at, m.created_at, m.kind, m.event_id,
    {user_columns("m.sender_id", "p", "s_")}
"""


async def thread(
    db: AsyncSession,
    conversation_id: str,
    me: str,
    *,
    after: Any,
    after_id: Optional[str],
    before: Any,
    before_id: Optional[str],
    limit: int,
    changed_since: Any = None,
) -> tuple[list[dict[str, Any]], bool, list[dict[str, Any]]]:
    """Messages oldest -> newest, whether more exist beyond the page, and what changed.

    `after` (polling) reads forward from a point; otherwise the newest page, or the
    page before `before`. `after_id`/`before_id` break ties on equal timestamps.
    `changed_since` also returns older messages edited or deleted since then (the
    poll's `polled_at`), in the same query: two index range scans, no more.
    """
    params: dict[str, Any] = {"c": conversation_id, "me": me, "limit": limit + 1}
    if after is not None:
        if after_id:
            cond = "(m.created_at, m.id) > (:after, cast(:after_id as uuid))"
            params["after_id"] = after_id
        else:
            cond = "m.created_at > :after"
        params["after"] = after
        order = "m.created_at, m.id"
    else:
        cond = "true"
        if before is not None:
            if before_id:
                cond = "(m.created_at, m.id) < (:before, cast(:before_id as uuid))"
                params["before_id"] = before_id
            else:
                cond = "m.created_at < :before"
            params["before"] = before
        order = "m.created_at desc, m.id desc"
    page = f"""
        (select {MESSAGE_COLUMNS}, false as changed
           from public.messages m
           left join public.profiles p on p.id = m.sender_id
          where m.conversation_id = :c and {cond} and {NOT_BLOCKED_SENDER}
          order by {order}
          limit :limit)
    """
    changes = ""
    if changed_since is not None:
        params["since"] = changed_since
        params["max_changes"] = MAX_CHANGES_PER_POLL
        outside_page = f" and not ({cond})" if after is not None else ""
        changes = f"""
        union all
        (select {MESSAGE_COLUMNS}, true as changed
           from public.messages m
           left join public.profiles p on p.id = m.sender_id
          where m.conversation_id = :c and m.updated_at is not null
            and m.updated_at > cast(:since as timestamptz) - interval '{CHANGE_OVERLAP_SECONDS} seconds'
            and {NOT_BLOCKED_SENDER}{outside_page}
          order by m.updated_at
          limit :max_changes)
        """
    rows = (await db.execute(text(page + changes), params)).mappings().all()
    fresh = [r for r in rows if not r["changed"]]
    more = len(fresh) > limit
    fresh = fresh[:limit]
    if after is None:
        fresh = list(reversed(fresh))
    changed = [r for r in rows if r["changed"]]
    out = await messages_out(db, [*fresh, *changed], conversation_id)
    return out[: len(fresh)], more, out[len(fresh) :]


async def mark_read(db: AsyncSession, conversation_id: str, me: str) -> None:
    await db.execute(
        text(
            """
            insert into public.conversation_members (conversation_id, user_id, last_read_at)
            values (:c, :me, now())
            on conflict (conversation_id, user_id) do update set last_read_at = now()
            """
        ),
        {"c": conversation_id, "me": me},
    )


async def check_rate(db: AsyncSession, uid: str) -> None:
    """At most MESSAGES_PER_MINUTE sends and edits together in any minute. One query, two index ranges."""
    recent = (
        await db.execute(
            text(
                "select (select count(*) from public.messages"
                "         where sender_id = :me and created_at > now() - interval '1 minute')"
                "     + (select count(*) from public.message_edits"
                "         where editor_id = :me and edited_at > now() - interval '1 minute')"
            ),
            {"me": uid},
        )
    ).scalar() or 0
    if recent >= MESSAGES_PER_MINUTE:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "You're sending messages too fast — wait a moment",
            headers={"Retry-After": "60"},
        )


async def edit(db: AsyncSession, message_id: UUID, me: dict, body: str) -> dict[str, Any]:
    """Change my own message's text while I can still send in its conversation. Its link rows
    follow the new text."""
    uid = str(me["id"])
    row = (
        await db.execute(
            text("select conversation_id, sender_id, deleted_at, kind from public.messages where id = :id"),
            {"id": str(message_id)},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Message not found")
    if str(row["sender_id"]) != uid:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You can only edit your own messages")
    if row["deleted_at"] is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This message was deleted")
    if not body and row["kind"] not in ("media", "event"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "A message is 1 to 2000 characters")
    c = await open_conversation(db, row["conversation_id"], uid)
    if not can_post(c):
        raise HTTPException(status.HTTP_403_FORBIDDEN, POST_REFUSED)
    other = str(c["o_id"]) if c["kind"] == "direct" and c["o_id"] else None
    if other and not await share_a_group(db, uid, other):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "You can only message people you share a circle or community with",
        )
    await check_rate(db, uid)
    cid = str(row["conversation_id"])
    updated = (
        await db.execute(
            text(
                f"""
                with m as (
                  update public.messages
                     set body = :b, edited_at = now(), updated_at = now()
                   where id = :id and deleted_at is null
                  returning id, body, deleted_at, edited_at, created_at, sender_id, kind, event_id
                ), logged as (
                  insert into public.message_edits (message_id, editor_id)
                  select id, sender_id from m
                )
                select {MESSAGE_COLUMNS}
                  from m left join public.profiles p on p.id = m.sender_id
                """
            ),
            {"id": str(message_id), "b": body},
        )
    ).mappings().first()
    if not updated:
        raise HTTPException(status.HTTP_409_CONFLICT, "This message was deleted")
    await message_links.sync(db, str(message_id), cid, body)
    return (await messages_out(db, [updated], cid))[0]


async def send(
    db: AsyncSession,
    c: dict[str, Any],
    me: dict,
    body: str,
    attachments: Optional[list[AttachmentIn]] = None,
) -> dict[str, Any]:
    """Store a message (rate-limited) with its attachments and links, mark it read for me, push a
    DM if it's due, and for an Announcements channel tell its members in the bell."""
    uid = str(me["id"])
    attachments = attachments or []
    if not can_post(c):
        raise HTTPException(status.HTTP_403_FORBIDDEN, POST_REFUSED)
    files.check_attachments(attachments, uid)
    other = str(c["o_id"]) if c["kind"] == "direct" and c["o_id"] else None
    if other and not await share_a_group(db, uid, other):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "You can only message people you share a circle or community with",
        )
    await check_rate(db, uid)
    cid = str(c["id"])
    row = (
        await db.execute(
            text(
                f"""
                with m as (
                  insert into public.messages (conversation_id, sender_id, body, kind)
                  values (:c, :me, :b, :kind)
                  returning id, body, deleted_at, edited_at, created_at, sender_id, kind, event_id
                ), touched as (
                  update public.conversations set last_message_at = (select created_at from m)
                   where id = :c
                )
                select {MESSAGE_COLUMNS}
                  from m left join public.profiles p on p.id = m.sender_id
                """
            ),
            {"c": cid, "me": uid, "b": body, "kind": "media" if attachments else "text"},
        )
    ).mappings().first()
    await mark_read(db, cid, uid)
    await files.save(db, str(row["id"]), cid, attachments)
    await message_links.sync(db, str(row["id"]), cid, body)

    if c["kind"] == "channel" and c["is_announcements"]:
        # The same bell as `POST /groups/{id}/announcements`: one INSERT ... SELECT, no email, no push.
        await inbox.notify_group(
            db,
            c["group_id"],
            "announcement",
            f"New in {c['parent_name'] or c['group_name']}",
            body=(body or "Shared an attachment")[:200],
            link=group_path(
                {
                    "kind": "channel",
                    "slug": c["group_slug"],
                    "parent_slug": c["parent_slug"],
                }
            ),
            actor_id=uid,
        )

    if other:
        # Push only when they have no unread push from this conversation in the last 10 minutes.
        due = (
            await db.execute(
                text(
                    f"""
                    insert into public.conversation_members (conversation_id, user_id, last_push_at)
                    values (:c, :o, now())
                    on conflict (conversation_id, user_id) do update set last_push_at = now()
                     where conversation_members.last_push_at is null
                        or conversation_members.last_push_at
                             < now() - interval '{DM_PUSH_QUIET_MINUTES} minutes'
                        or conversation_members.last_read_at >= conversation_members.last_push_at
                    returning user_id
                    """
                ),
                {"c": cid, "o": other},
            )
        ).first()
        if due:
            sender = (me.get("full_name") or "").strip() or "Someone on ORBIT"
            message = direct_message_push(sender, body or "Sent an attachment", f"/messages/{cid}", cid)
            try:
                _after_commit(
                    db,
                    f"dm push {cid}",
                    partial(_push, user_id=UUID(other), message=message),
                )
            except Exception:
                logger.exception("[messages] dm push not queued")
    return (await messages_out(db, [row], cid))[0]


async def delete(db: AsyncSession, message_id: UUID, me: dict) -> None:
    """Delete a message: it stays in the thread as "Message deleted", without its text, and
    leaves the Media, Links and Docs tabs. Its sender, or in a channel anyone who can manage it.
    Its files on Cloudinary go after the commit, best effort."""
    uid = str(me["id"])
    row = (
        await db.execute(
            text(
                """
                select m.sender_id, m.deleted_at, c.kind, gm.role as my_role, pm.role as parent_role
                  from public.messages m
                  join public.conversations c on c.id = m.conversation_id
                  left join public.groups g on g.id = c.group_id
                  left join public.group_members gm
                    on gm.group_id = c.group_id and gm.user_id = cast(:me as uuid)
                  left join public.group_members pm
                    on pm.group_id = g.parent_id and pm.user_id = cast(:me as uuid)
                 where m.id = :id
                """
            ),
            {"id": str(message_id), "me": uid},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Message not found")
    mine = row["sender_id"] is not None and str(row["sender_id"]) == uid
    moderator = can_moderate({**row, "orbit_role": me["role"]})
    if not (mine or moderator):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You can only delete your own messages")
    if row["deleted_at"] is not None:
        return
    await db.execute(
        text(
            "update public.messages set deleted_at = now(), body = '', updated_at = now()"
            " where id = :id"
        ),
        {"id": str(message_id)},
    )
    await db.execute(text("delete from public.message_links where message_id = :id"), {"id": str(message_id)})
    urls = (
        await db.execute(
            text("delete from public.message_attachments where message_id = :id returning url"),
            {"id": str(message_id)},
        )
    ).scalars().all()
    if urls:
        await release_images(db, urls)


async def release_group_files(db: AsyncSession, group_id: str) -> None:
    """Before a group is deleted: its chat files go too, after the commit, best effort. The
    rows themselves cascade; these go first, so the "still used" check doesn't find them."""
    urls = (
        await db.execute(
            text(
                "delete from public.message_attachments a using public.conversations c"
                " where c.id = a.conversation_id and c.group_id = :g returning a.url"
            ),
            {"g": group_id},
        )
    ).scalars().all()
    if urls:
        await release_images(db, urls)


# ─── group conversations and event messages ─────────────────────────────────


async def group_conversation(db: AsyncSession, g: dict, uid: Optional[str]) -> str:
    """The conversation of a circle or channel, created on first use."""
    return str(
        (
            await db.execute(
                text(
                    """
                    insert into public.conversations (kind, group_id, created_by)
                    values (:kind, :g, cast(:u as uuid))
                    on conflict (group_id) do update set kind = excluded.kind
                    returning id
                    """
                ),
                {"kind": g["kind"], "g": g["id"], "u": uid},
            )
        ).scalar_one()
    )


async def post_shared_event(
    db: AsyncSession, g: dict, event_id: str, uid: str, note: Optional[str]
) -> None:
    """An event shared to a channel shows in its chat. One message per event per conversation:
    if the daily auto-post already made it, nothing changes; if it was deleted, it comes back."""
    cid = await group_conversation(db, g, uid)
    await db.execute(
        text(
            """
            with m as (
              insert into public.messages as x (conversation_id, sender_id, body, kind, event_id)
              values (:c, :u, :b, 'event', :e)
              on conflict (conversation_id, event_id) where kind = 'event'
              do update set deleted_at = null, updated_at = now(), created_at = now(),
                            sender_id = excluded.sender_id, body = excluded.body
               where x.deleted_at is not null
              returning created_at
            )
            update public.conversations
               set last_message_at = greatest(last_message_at, (select created_at from m))
             where id = :c and exists (select 1 from m)
            """
        ),
        {"c": cid, "u": uid, "b": (note or "").strip(), "e": event_id},
    )


async def retract_shared_event(db: AsyncSession, group_id: str, event_id: str) -> None:
    """Un-sharing takes its chat message with it. An auto-posted one (no sender) stays: the
    event still matches the channel's categories."""
    await db.execute(
        text(
            """
            update public.messages m
               set deleted_at = now(), body = '', updated_at = now()
              from public.conversations c
             where c.group_id = :g and m.conversation_id = c.id and m.event_id = :e
               and m.kind = 'event' and m.sender_id is not null and m.deleted_at is null
            """
        ),
        {"g": group_id, "e": event_id},
    )


# ─── the library: Media, Links and Docs ─────────────────────────────────────


async def library(
    db: AsyncSession,
    conversation_id: str,
    me: str,
    *,
    tab: str,
    q: Optional[str],
    limit: int,
    cursor: Optional[str],
) -> dict[str, Any]:
    """Newest first, keyset on (created_at, id). `q` matches name and url, or for links title,
    url and domain. Deleted messages and people I blocked are left out. Links are link rows plus
    event messages, each branch ordered and limited on its own, so both start at the cursor."""
    params: dict[str, Any] = {"c": conversation_id, "me": me, "limit": limit + 1}
    after = decode_cursor(cursor, "tu")
    if q and q.strip():
        params["q"] = f"%{escape_like(q.strip().lower())}%"
    if tab == "links":
        rows = (await db.execute(text(_library_links(params, after)), params)).mappings().all()
    else:
        params["kind"] = "image" if tab == "media" else "doc"
        rows = (await db.execute(text(_library_files(params, after)), params)).mappings().all()
    more = len(rows) > limit
    rows = rows[:limit]
    cards = await _event_cards(
        db, list({str(r["event_id"]) for r in rows if r.get("event_id")})
    )
    items = []
    for r in rows:
        item: dict[str, Any] = {
            "id": str(r["id"]),
            "message_id": str(r["message_id"]),
            "sender": user_brief(r, "s_"),
            "created_at": r["created_at"],
        }
        if tab != "links":
            item.update(files.attachment_out(r))
        elif r["item_type"] == "event":
            item.update(
                type="event",
                url=f"/events/{r['event_id']}",
                domain=None,
                title=r["title"],
                description=None,
                image_url=None,
                event=cards.get(str(r["event_id"])),
            )
        else:
            item.update(type="link", event=None, **{k: v for k, v in message_links.link_out(r).items() if k != "id"})
        items.append(item)
    next_cursor = encode_cursor(rows[-1]["created_at"], rows[-1]["id"]) if more and rows else None
    return {"items": items, "next_cursor": next_cursor, "page_size": limit}


def _keyset(alias: str, params: dict[str, Any], after: Optional[list[Any]]) -> str:
    if not after:
        return ""
    params.update(c_at=after[0], c_id=after[1])
    return f" and ({alias}.created_at, {alias}.id) < (:c_at, cast(:c_id as uuid))"


def _library_files(params: dict[str, Any], after: Optional[list[Any]]) -> str:
    search = ""
    if "q" in params:
        search = " and (lower(a.name) like :q escape '\\' or lower(a.url) like :q escape '\\')"
    return f"""
        select a.id, a.message_id, a.kind, a.url, a.public_id, a.name, a.mime, a.bytes,
               a.width, a.height, a.created_at, {user_columns("m.sender_id", "p", "s_")}
          from public.message_attachments a
          join public.messages m on m.id = a.message_id
          left join public.profiles p on p.id = m.sender_id
         where a.conversation_id = :c and a.kind = :kind and m.deleted_at is null
           and {NOT_BLOCKED_SENDER}{_keyset("a", params, after)}{search}
         order by a.created_at desc, a.id desc
         limit :limit
    """


def _library_links(params: dict[str, Any], after: Optional[list[Any]]) -> str:
    link_search = event_search = ""
    if "q" in params:
        link_search = (
            " and (lower(l.url) like :q escape '\\' or lower(l.title) like :q escape '\\'"
            " or lower(l.domain) like :q escape '\\')"
        )
        event_search = " and lower(e.title) like :q escape '\\'"
    return f"""
        select * from (
          (select 'link' as item_type, l.id, l.message_id, l.created_at, l.url, l.domain, l.title,
                  l.description, l.image_url, null::uuid as event_id,
                  {user_columns("m.sender_id", "p", "s_")}
             from public.message_links l
             join public.messages m on m.id = l.message_id
             left join public.profiles p on p.id = m.sender_id
            where l.conversation_id = :c and m.deleted_at is null
              and {NOT_BLOCKED_SENDER}{_keyset("l", params, after)}{link_search}
            order by l.created_at desc, l.id desc
            limit :limit)
          union all
          (select 'event', m.id, m.id, m.created_at, null, null, e.title, null, null, m.event_id,
                  {user_columns("m.sender_id", "p", "s_")}
             from public.messages m
             join public.events e on e.id = m.event_id
             left join public.organisers o on o.id = e.organiser_id
             left join public.profiles p on p.id = m.sender_id
            where m.conversation_id = :c and m.kind = 'event' and m.deleted_at is null
              and e.status in ('published', 'cancelled', 'expired') and {EVENT_VISIBLE}
              and {NOT_BLOCKED_SENDER}{_keyset("m", params, after)}{event_search}
            order by m.created_at desc, m.id desc
            limit :limit)
        ) x
        order by x.created_at desc, x.id desc
        limit :limit
    """


async def _push(db: AsyncSession, user_id: UUID, message: Any) -> None:
    recipients = await load_recipients(db, [user_id])
    if "push" not in channels_for(user_id, recipients.prefs(user_id), recipients, channels_available()):
        return
    sent = await send_push(recipients.subscriptions.get(user_id, []), message)
    await delete_subscriptions(db, sent.expired_endpoints)
