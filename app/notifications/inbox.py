"""The in-app inbox (the bell): rows in `notifications`, plus push/email for invites.

Features call `notify` (named people) or `notify_group` (a group's members or
admins) inside their own transaction, so a change that rolls back leaves no
notification behind. Only invites go further than the bell: `deliver=True`
queues a push and an email for after the commit, each following the person's
notification preferences. Everything else (join requests, approvals, shared
events, announcements) is in-app only — no email spam.
"""

import logging
from functools import partial
from typing import Any, Literal, Optional, Sequence
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import decode_cursor, encode_cursor
from app.notifications.channels import (
    app_url,
    channels_available,
    delete_subscriptions,
    send_emails,
    send_push,
)
from app.notifications.messages import Message, render_email
from app.notifications.service import _after_commit, channels_for, load_recipients
from app.users.brief import user_brief, user_columns

logger = logging.getLogger(__name__)

Kind = Literal["circle_invite", "join_request", "request_approved", "event_shared", "announcement"]

INSERT = """
insert into public.notifications
  (user_id, kind, title, body, link, actor_id, group_id, invite_id)
select u, :kind, :title, :body, :link,
       cast(:actor as uuid), cast(:group_id as uuid), cast(:invite_id as uuid)
  from unnest(cast(:ids as uuid[])) as u
"""


def _params(kind: str, title: str, body, link, actor_id, group_id, invite_id) -> dict[str, Any]:
    return {
        "kind": kind,
        "title": title[:200],
        "body": body[:500] if body else body,
        "link": link,
        "actor": str(actor_id) if actor_id else None,
        "group_id": str(group_id) if group_id else None,
        "invite_id": str(invite_id) if invite_id else None,
    }


async def notify(
    db: AsyncSession,
    user_ids: Sequence[Any],
    kind: Kind,
    title: str,
    *,
    body: Optional[str] = None,
    link: Optional[str] = None,
    actor_id: Any = None,
    group_id: Any = None,
    invite_id: Any = None,
    deliver: Optional[Message] = None,
) -> None:
    """Put a notification in each person's inbox; `deliver` also pushes/emails it after commit."""
    ids = [str(u) for u in dict.fromkeys(user_ids) if u and str(u) != str(actor_id or "")]
    if not ids:
        return
    await db.execute(
        text(INSERT),
        {"ids": ids, **_params(kind, title, body, link, actor_id, group_id, invite_id)},
    )
    if deliver is not None:
        try:
            people = [UUID(i) for i in ids]
            _after_commit(
                db,
                f"inbox deliver {kind} to {len(people)}",
                partial(_deliver, user_ids=people, message=deliver),
            )
        except Exception:
            logger.exception("[inbox] %s delivery not queued", kind)


async def notify_group(
    db: AsyncSession,
    group_id: Any,
    kind: Kind,
    title: str,
    *,
    admins_only: bool = False,
    body: Optional[str] = None,
    link: Optional[str] = None,
    actor_id: Any = None,
    invite_id: Any = None,
    announcement_id: Any = None,
) -> None:
    """In-app only: every member (or every admin) of a group except the actor."""
    role = " and m.role = 'admin'" if admins_only else ""
    await db.execute(
        text(
            f"""
            insert into public.notifications
              (user_id, kind, title, body, link, actor_id, group_id, invite_id, announcement_id)
            select m.user_id, :kind, :title, :body, :link,
                   cast(:actor as uuid), cast(:group_id as uuid), cast(:invite_id as uuid),
                   cast(:announcement_id as uuid)
              from public.group_members m
             where m.group_id = cast(:group_id as uuid){role}
               and m.user_id is distinct from cast(:actor as uuid)
            """
        ),
        {
            **_params(kind, title, body, link, actor_id, group_id, invite_id),
            "announcement_id": str(announcement_id) if announcement_id else None,
        },
    )


async def mark_invite_read(db: AsyncSession, invite_id: Any) -> None:
    """An invite or join request was answered: its notifications have nothing left to ask."""
    await db.execute(
        text(
            "update public.notifications set read_at = now()"
            " where invite_id = :i and read_at is null"
        ),
        {"i": str(invite_id)},
    )


async def _deliver(db: AsyncSession, user_ids: list[UUID], message: Message) -> None:
    recipients = await load_recipients(db, user_ids)
    available = channels_available()
    expired: list[str] = []
    emails = []
    pushed = 0
    base = app_url()
    for user_id in user_ids:
        channels = channels_for(user_id, recipients.prefs(user_id), recipients, available)
        if "push" in channels:
            sent = await send_push(recipients.subscriptions.get(user_id, []), message)
            expired.extend(sent.expired_endpoints)
            pushed += sent.delivered > 0
        if "email" in channels:
            emails.append((recipients.emails[user_id], render_email(message, base)))
    await delete_subscriptions(db, expired)
    ok = await send_emails(emails)
    logger.info(
        "[inbox] %s: %d/%d pushed, %d/%d emailed",
        message.tag,
        pushed,
        len(user_ids),
        sum(ok),
        len(emails),
    )


# ─── reading ────────────────────────────────────────────────────────────────


async def unread_count(db: AsyncSession, user_id: str) -> int:
    return int(
        (
            await db.execute(
                text(
                    "select count(*) from public.notifications"
                    " where user_id = :u and read_at is null"
                ),
                {"u": user_id},
            )
        ).scalar()
        or 0
    )


async def list_inbox(
    db: AsyncSession, user_id: str, *, unread_only: bool, limit: int, cursor: Optional[str]
) -> dict[str, Any]:
    """Newest first, keyset on (created_at, id); one query for the page, one for the count."""
    where = ["n.user_id = :u"]
    params: dict[str, Any] = {"u": user_id, "limit": limit + 1}
    if unread_only:
        where.append("n.read_at is null")
    after = decode_cursor(cursor, "tu")
    if after:
        where.append("(n.created_at, n.id) < (:c_at, cast(:c_id as uuid))")
        params.update(c_at=after[0], c_id=after[1])
    rows = (
        await db.execute(
            text(
                f"""
                select n.id, n.kind, n.title, n.body, n.link, n.group_id, n.invite_id,
                       n.read_at, n.created_at,
                       {user_columns("n.actor_id", "ap", "actor_")}
                  from public.notifications n
                  left join public.profiles ap on ap.id = n.actor_id
                 where {" and ".join(where)}
                 order by n.created_at desc, n.id desc
                 limit :limit
                """
            ),
            params,
        )
    ).mappings().all()
    more = len(rows) > limit
    rows = rows[:limit]
    items = [
        {
            "id": str(r["id"]),
            "kind": r["kind"],
            "title": r["title"],
            "body": r["body"],
            "link": r["link"],
            "actor": user_brief(r, "actor_"),
            "group_id": str(r["group_id"]) if r["group_id"] else None,
            "invite_id": str(r["invite_id"]) if r["invite_id"] else None,
            "read_at": r["read_at"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]
    next_cursor = encode_cursor(rows[-1]["created_at"], rows[-1]["id"]) if more and rows else None
    return {
        "items": items,
        "unread_count": await unread_count(db, user_id),
        "next_cursor": next_cursor,
    }


async def mark_read(db: AsyncSession, user_id: str, notification_id: str) -> bool:
    row = (
        await db.execute(
            text(
                "update public.notifications set read_at = coalesce(read_at, now())"
                " where id = :id and user_id = :u returning id"
            ),
            {"id": notification_id, "u": user_id},
        )
    ).first()
    return row is not None


async def mark_all_read(db: AsyncSession, user_id: str) -> None:
    await db.execute(
        text(
            "update public.notifications set read_at = now()"
            " where user_id = :u and read_at is null"
        ),
        {"u": user_id},
    )
