"""Circle chat and direct messages: access, the inbox queries, sending.

Access is decided live on every request:

* circle chat — the caller must be in `group_members` for that circle now;
* direct — the caller is one of the pair and neither has blocked the other;
  starting one or sending needs a group in common (no cold messages).

The two polling paths stay cheap: `unread_total` is one aggregate over the
caller's conversations (each count an index range on messages), and a thread
poll (`after=`) is one access query plus one index range scan.
"""

import logging
from functools import partial
from typing import Any, Optional
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import decode_cursor, encode_cursor
from app.notifications.channels import channels_available, delete_subscriptions, send_push
from app.notifications.messages import direct_message_push
from app.notifications.service import _after_commit, channels_for, load_recipients
from app.users.brief import user_brief, user_columns

logger = logging.getLogger(__name__)

MESSAGES_PER_MINUTE = 20
DM_PUSH_QUIET_MINUTES = 10

# Messages from someone I blocked are hidden from me everywhere.
NOT_BLOCKED_SENDER = """
    not exists (select 1 from public.user_blocks b
                 where b.blocker_id = cast(:me as uuid) and b.blocked_id = m.sender_id)
"""

# Every conversation I can open, with the moment after which a message counts as unread
# (`since`: my last read, and for a circle not before I joined) and its last activity.
MINE = """
mine as (
  select c.id, c.kind, c.group_id, null::uuid as other_id,
         greatest(coalesce(cm.last_read_at, '-infinity'::timestamptz), gm.created_at) as since,
         coalesce(c.last_message_at, c.created_at) as activity
    from public.group_members gm
    join public.conversations c on c.group_id = gm.group_id
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
    deleted = r["deleted_at"] is not None
    return {
        "id": str(r["id"]),
        "conversation_id": conversation_id or str(r["conversation_id"]),
        "sender": user_brief(r, "s_"),
        "body": "" if deleted else r["body"],
        "deleted": deleted,
        "created_at": r["created_at"],
    }


def _item(r: Any) -> dict[str, Any]:
    last = None
    if r["lm_at"] is not None:
        last = {
            "body": r["lm_body"],
            "sender_id": str(r["lm_sender"]) if r["lm_sender"] else None,
            "created_at": r["lm_at"],
        }
    return {
        "id": str(r["id"]),
        "kind": r["kind"],
        "group": {"id": str(r["group_id"]), "name": r["group_name"]} if r["group_id"] else None,
        "other_user": user_brief(r, "o_") if r["kind"] == "direct" else None,
        "last_message": last,
        "unread_count": int(r["unread"] or 0),
        "last_activity_at": r["activity"],
    }


# ─── the conversation list and the badge ────────────────────────────────────


async def unread_total(db: AsyncSession, me: str) -> int:
    """One aggregate query: unread messages across every conversation I can open."""
    return int(
        (
            await db.execute(
                text(f"with {MINE} select coalesce(sum(u.n), 0) from mine {UNREAD_LATERAL}"),
                {"me": me},
            )
        ).scalar()
        or 0
    )


async def conversations(
    db: AsyncSession, me: str, *, limit: int, cursor: Optional[str], only: Optional[str] = None
) -> dict[str, Any]:
    """Most recent activity first; keyset on (activity, id)."""
    params: dict[str, Any] = {"me": me, "limit": limit + 1}
    where = ["true"]
    after = decode_cursor(cursor, "tu")
    if after:
        where.append("(mine.activity, mine.id) < (:c_at, cast(:c_id as uuid))")
        params.update(c_at=after[0], c_id=after[1])
    if only:
        where.append("mine.id = cast(:only as uuid)")
        params["only"] = only
    rows = (
        await db.execute(
            text(
                f"""
                with {MINE}
                select mine.id, mine.kind, mine.group_id, mine.activity, g.name as group_name,
                       {user_columns("mine.other_id", "op", "o_")},
                       lm.body as lm_body, lm.sender_id as lm_sender, lm.created_at as lm_at,
                       u.n as unread
                  from mine
                  left join public.groups g on g.id = mine.group_id
                  left join public.profiles op on op.id = mine.other_id
                  left join lateral (
                    select m.body, m.sender_id, m.created_at
                      from public.messages m
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
                select c.id, c.kind, c.group_id, g.name as group_name,
                       gm.created_at as joined_at,
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
                  left join public.group_members gm
                    on gm.group_id = c.group_id and gm.user_id = cast(:me as uuid)
                  left join public.profiles op on op.id = x.other_id
                 where c.id = :id
                """
            ),
            {"id": str(conversation_id), "me": me},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    if row["kind"] == "circle":
        if row["joined_at"] is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "You're not in this circle")
    else:
        if not row["participant"]:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
        if row["blocked"]:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "You can't message this person")
    return dict(row)


def slim(c: dict[str, Any]) -> dict[str, Any]:
    """The conversation fields a poll returns (no counts, no member list)."""
    return {
        "id": str(c["id"]),
        "kind": c["kind"],
        "group": {"id": str(c["group_id"]), "name": c["group_name"]} if c["group_id"] else None,
        "other_user": user_brief(c, "o_") if c["kind"] == "direct" else None,
    }


async def share_a_group(db: AsyncSession, a: str, b: str) -> bool:
    return bool(
        (
            await db.execute(
                text(
                    "select exists (select 1 from public.group_members x"
                    " join public.group_members y on y.group_id = x.group_id"
                    " where x.user_id = :a and y.user_id = :b)"
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
    if c["kind"] == "circle":
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
        "id": me["id"],
        "full_name": me.get("full_name"),
        "avatar_url": me.get("avatar_url"),
        "university": me.get("university"),
    }
    return [myself, user_brief(c, "o_")]


# ─── the thread ─────────────────────────────────────────────────────────────


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
) -> tuple[list[dict[str, Any]], bool]:
    """Messages oldest -> newest, and whether more exist beyond the page.

    `after` (polling) reads forward from a point; otherwise the newest page, or the
    page before `before`. `after_id`/`before_id` break ties on equal timestamps.
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
    rows = (
        await db.execute(
            text(
                f"""
                select m.id, m.body, m.deleted_at, m.created_at,
                       {user_columns("m.sender_id", "p", "s_")}
                  from public.messages m
                  left join public.profiles p on p.id = m.sender_id
                 where m.conversation_id = :c and {cond} and {NOT_BLOCKED_SENDER}
                 order by {order}
                 limit :limit
                """
            ),
            params,
        )
    ).mappings().all()
    more = len(rows) > limit
    rows = rows[:limit]
    if after is None:
        rows = list(reversed(rows))
    return [message_out(r, conversation_id) for r in rows], more


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


async def send(db: AsyncSession, c: dict[str, Any], me: dict, body: str) -> dict[str, Any]:
    """Store a message (rate-limited), mark it read for me, and push a DM if it's due."""
    uid = me["id"]
    other = str(c["o_id"]) if c["kind"] == "direct" and c["o_id"] else None
    if other and not await share_a_group(db, uid, other):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "You can only message people you share a circle or community with",
        )
    recent = (
        await db.execute(
            text(
                "select count(*) from public.messages"
                " where sender_id = :me and created_at > now() - interval '1 minute'"
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
    cid = str(c["id"])
    row = (
        await db.execute(
            text(
                f"""
                with m as (
                  insert into public.messages (conversation_id, sender_id, body)
                  values (:c, :me, :b)
                  returning id, body, deleted_at, created_at, sender_id
                ), touched as (
                  update public.conversations set last_message_at = (select created_at from m)
                   where id = :c
                )
                select m.id, m.body, m.deleted_at, m.created_at,
                       {user_columns("m.sender_id", "p", "s_")}
                  from m left join public.profiles p on p.id = m.sender_id
                """
            ),
            {"c": cid, "me": uid, "b": body},
        )
    ).mappings().first()
    await mark_read(db, cid, uid)

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
            message = direct_message_push(sender, body, f"/messages/{cid}", cid)
            try:
                _after_commit(
                    db,
                    f"dm push {cid}",
                    partial(_push, user_id=UUID(other), message=message),
                )
            except Exception:
                logger.exception("[messages] dm push not queued")
    return message_out(row, cid)


async def _push(db: AsyncSession, user_id: UUID, message: Any) -> None:
    recipients = await load_recipients(db, [user_id])
    if "push" not in channels_for(user_id, recipients.prefs(user_id), recipients, channels_available()):
        return
    sent = await send_push(recipients.subscriptions.get(user_id, []), message)
    await delete_subscriptions(db, sent.expired_endpoints)
