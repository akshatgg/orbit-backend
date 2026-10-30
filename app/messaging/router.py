"""Circle and channel chat, and direct messages (polling, no websockets).

The open thread polls `GET /messages/conversations/{id}?after=<created_at of the
newest message>&changed_since=<polled_at of the last response>` (new messages in
`items`, edits and deletes of older ones in `changed`); the nav badge polls
`GET /messages/unread-count`. Messages never go to the notifications inbox; a
direct message sends a web push only when the recipient has no unread push from
that conversation in the last 10 minutes. A post in an Announcements channel is
the one exception: it reaches its members' bell, as a community announcement does.

The conversation list and the badge are circles and direct messages only; a channel's
unread count is on `GET /groups/{community}/channels`.
"""

from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import require_user
from app.core.pagination import CURSOR_LIMIT_MAX
from app.groups import service as gs
from app.messaging import service as ms
from app.messaging.schemas import BlockCreate, DirectStart, MessageCreate, MessageUpdate
from app.users.brief import user_brief, user_columns

router = APIRouter(prefix="/messages", tags=["messages"])
group_chat_router = APIRouter(prefix="/groups", tags=["messages"])


@router.get("/conversations")
async def list_conversations(
    limit: int = Query(30, ge=1, le=CURSOR_LIMIT_MAX),
    cursor: Optional[str] = Query(None, max_length=500),
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """My circle chats and direct conversations, most recent activity first, with unread counts.

    Pages by keyset: pass `next_cursor` back as `cursor`.
    """
    page = await ms.conversations(db, str(user["id"]), limit=limit, cursor=cursor)
    page["unread_total"] = await ms.unread_total(db, str(user["id"]))
    return page


@router.get("/unread-count")
async def unread_count(
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Unread messages across all my conversations (the nav badge)."""
    return {"unread_total": await ms.unread_total(db, str(user["id"]))}


@router.post("/direct")
async def start_direct(
    body: DirectStart,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get or create my direct conversation with someone I share a circle, community or channel
    with. An `auto_join` group doesn't count.

    403 when we share no group, or either of us blocked the other.
    """
    other = gs.as_uuid(body.user_id, "user id")
    if other == str(user["id"]):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "That's you")
    exists = (
        await db.execute(text("select 1 from public.users where id = :id"), {"id": other})
    ).first()
    if not exists:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if await ms.blocked_either_way(db, str(user["id"]), other):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You can't message this person")
    if not await ms.share_a_group(db, str(user["id"]), other):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "You can only message people you share a circle or community with",
        )
    conversation_id = (
        await db.execute(
            text(
                """
                insert into public.conversations (kind, user_a, user_b, created_by)
                values ('direct', least(cast(:a as uuid), cast(:b as uuid)),
                        greatest(cast(:a as uuid), cast(:b as uuid)), cast(:a as uuid))
                on conflict (user_a, user_b) do update set kind = excluded.kind
                returning id
                """
            ),
            {"a": str(user["id"]), "b": other},
        )
    ).scalar_one()
    return {"id": str(conversation_id)}


@group_chat_router.get("/{group_id}/conversation")
async def group_conversation(
    group_id: str,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The chat of a circle or channel (created on first use). Members only; communities have
    no chat. A private channel I can't see is a 404."""
    g = await gs.resolve(db, group_id, user)
    if g["kind"] == "community":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Only circles and channels have a chat")
    role = await gs.role_of(db, g["id"], user)
    if not role:
        raise gs.deny_read(g, role, user, f"You're not in this {g['kind']}")
    return {"id": await ms.group_conversation(db, g, str(user["id"]))}


@router.get("/conversations/{conversation_id}")
async def get_thread(
    conversation_id: UUID,
    after: Optional[datetime] = None,
    after_id: Optional[UUID] = None,
    before: Optional[datetime] = None,
    before_id: Optional[UUID] = None,
    changed_since: Optional[datetime] = None,
    limit: int = Query(50, ge=1, le=CURSOR_LIMIT_MAX),
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Messages oldest -> newest: the newest page, the page `before` a point, or everything `after` one.

    With `after` (the polling path) `conversation` is the slim `{id, kind, group, other_user,
    can_post, can_moderate}`; otherwise it is the full list item plus those two and `members`
    (empty in an `auto_join` channel unless I can manage it). `has_more` says whether more
    messages lie beyond the page (older ones, or with `after` newer ones).

    Edits and deletes: pass the previous response's `polled_at` as `changed_since` and
    `changed` lists the messages edited or deleted since then (replace them by id).
    """
    c = await ms.open_conversation(db, conversation_id, str(user["id"]))
    items, more, changed = await ms.thread(
        db,
        str(conversation_id),
        str(user["id"]),
        after=after,
        after_id=str(after_id) if after_id else None,
        before=before,
        before_id=str(before_id) if before_id else None,
        limit=limit,
        changed_since=changed_since,
    )
    if after is not None:
        conversation = ms.slim(c)
    else:
        listed = await ms.conversations(
            db, str(user["id"]), limit=1, cursor=None, only=str(conversation_id)
        )
        conversation = listed["items"][0] if listed["items"] else ms.slim(c)
        conversation.update(ms.permissions(c))
        conversation["members"] = await ms.members(db, c, user)
    return {
        "conversation": conversation,
        "items": items,
        "has_more": more,
        "changed": changed,
        "polled_at": c["polled_at"],
    }


@router.post("/conversations/{conversation_id}", status_code=201)
async def send_message(
    conversation_id: UUID,
    body: MessageCreate,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Send a message (up to 2000 characters, at most 20 a minute), with up to 10 attachments.

    The text may be empty when there are attachments. 403 "Only moderators can send messages
    here" in a channel where I may not post. 422 for an attachment that isn't my own upload.
    """
    c = await ms.open_conversation(db, conversation_id, str(user["id"]))
    return await ms.send(db, c, user, body.body, body.attachments)


@router.get("/conversations/{conversation_id}/library")
async def conversation_library(
    conversation_id: UUID,
    tab: str = Query("media", pattern="^(media|links|docs)$"),
    q: Optional[str] = Query(None, max_length=100),
    limit: int = Query(30, ge=1, le=CURSOR_LIMIT_MAX),
    cursor: Optional[str] = Query(None, max_length=500),
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """A thread's Media, Links or Docs, newest first. Same access as reading the thread.

    Pages by keyset: pass `next_cursor` back as `cursor`.
    """
    await ms.open_conversation(db, conversation_id, str(user["id"]))
    return await ms.library(
        db, str(conversation_id), str(user["id"]), tab=tab, q=q, limit=limit, cursor=cursor
    )


@router.post("/conversations/{conversation_id}/read")
async def read_conversation(
    conversation_id: UUID,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Mark the conversation read up to now; returns the new badge total."""
    await ms.open_conversation(db, conversation_id, str(user["id"]))
    await ms.mark_read(db, str(conversation_id), str(user["id"]))
    return {"ok": True, "unread_total": await ms.unread_total(db, str(user["id"]))}


@router.patch("/{message_id}")
async def edit_message(
    message_id: UUID,
    body: MessageUpdate,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Edit my own message (1-2000 characters; a message with attachments or an event may go
    empty) while I can still send in its conversation.

    409 if it was deleted. Edits count toward the 20-a-minute limit; other people's open
    threads get the new text through `changed`.
    """
    return await ms.edit(db, message_id, user, body.body)


@router.delete("/{message_id}", status_code=204)
async def delete_message(
    message_id: UUID,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Delete my own message, or in a channel I manage, anyone's: it stays in the thread as
    "Message deleted", without its text or attachments, and leaves the library.

    Other people's open threads get it through `changed`."""
    await ms.delete(db, message_id, user)
    return Response(status_code=204)


# ─── blocks ─────────────────────────────────────────────────────────────────


@router.get("/blocks")
async def list_blocks(
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """People I've blocked."""
    rows = (
        await db.execute(
            text(
                f"""
                select b.created_at, {user_columns("b.blocked_id", "p", "u_")}
                  from public.user_blocks b
                  left join public.profiles p on p.id = b.blocked_id
                 where b.blocker_id = :me
                 order by b.created_at desc
                """
            ),
            {"me": str(user["id"])},
        )
    ).mappings().all()
    return {"items": [{"user": user_brief(r, "u_"), "created_at": r["created_at"]} for r in rows]}


@router.post("/blocks", status_code=201)
async def block_user(
    body: BlockCreate,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Block someone: no direct messages either way, and their circle-chat messages are hidden from me."""
    other = gs.as_uuid(body.user_id, "user id")
    if other == str(user["id"]):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "You can't block yourself")
    row = (
        await db.execute(
            text(
                f"""
                with b as (
                  insert into public.user_blocks (blocker_id, blocked_id)
                  select :me, u.id from public.users u where u.id = :o
                  on conflict do nothing
                  returning blocked_id, created_at
                )
                select coalesce(b.created_at, x.created_at) as created_at,
                       {user_columns("u.id", "p", "u_")}
                  from public.users u
                  left join b on b.blocked_id = u.id
                  left join public.user_blocks x on x.blocker_id = :me and x.blocked_id = u.id
                  left join public.profiles p on p.id = u.id
                 where u.id = :o
                """
            ),
            {"me": str(user["id"]), "o": other},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return {"user": user_brief(row, "u_"), "created_at": row["created_at"]}


@router.delete("/blocks/{user_id}", status_code=204)
async def unblock_user(
    user_id: UUID,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Unblock someone."""
    await db.execute(
        text("delete from public.user_blocks where blocker_id = :me and blocked_id = :o"),
        {"me": str(user["id"]), "o": str(user_id)},
    )
    return Response(status_code=204)
