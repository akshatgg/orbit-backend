"""Circle chat and direct messages (polling, no websockets).

The open thread polls `GET /messages/conversations/{id}?after=<created_at of the
newest message>`; the nav badge polls `GET /messages/unread-count`. Messages
never go to the notifications inbox; a direct message sends a web push only
when the recipient has no unread push from that conversation in the last 10
minutes.
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
from app.messaging.schemas import BlockCreate, DirectStart, MessageCreate
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
    """Get or create my direct conversation with someone I share a circle or community with.

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
async def circle_conversation(
    group_id: str,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The circle's chat (created on first use). Members only; communities have no chat."""
    g = await gs.resolve(db, group_id)
    if g["kind"] != "circle":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Only circles have a chat")
    if not await gs.role_of(db, g["id"], user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You're not in this circle")
    conversation_id = (
        await db.execute(
            text(
                """
                insert into public.conversations (kind, group_id, created_by)
                values ('circle', :g, :u)
                on conflict (group_id) do update set kind = excluded.kind
                returning id
                """
            ),
            {"g": g["id"], "u": str(user["id"])},
        )
    ).scalar_one()
    return {"id": str(conversation_id)}


@router.get("/conversations/{conversation_id}")
async def get_thread(
    conversation_id: UUID,
    after: Optional[datetime] = None,
    after_id: Optional[UUID] = None,
    before: Optional[datetime] = None,
    before_id: Optional[UUID] = None,
    limit: int = Query(50, ge=1, le=CURSOR_LIMIT_MAX),
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Messages oldest -> newest: the newest page, the page `before` a point, or everything `after` one.

    With `after` (the polling path) `conversation` is the slim `{id, kind, group, other_user}`;
    otherwise it is the full list item plus `members`. `has_more` says whether more
    messages lie beyond the page (older ones, or with `after` newer ones).
    """
    c = await ms.open_conversation(db, conversation_id, str(user["id"]))
    items, more = await ms.thread(
        db,
        str(conversation_id),
        str(user["id"]),
        after=after,
        after_id=str(after_id) if after_id else None,
        before=before,
        before_id=str(before_id) if before_id else None,
        limit=limit,
    )
    if after is not None:
        conversation = ms.slim(c)
    else:
        listed = await ms.conversations(
            db, str(user["id"]), limit=1, cursor=None, only=str(conversation_id)
        )
        conversation = listed["items"][0] if listed["items"] else ms.slim(c)
        conversation["members"] = await ms.members(db, c, user)
    return {"conversation": conversation, "items": items, "has_more": more}


@router.post("/conversations/{conversation_id}", status_code=201)
async def send_message(
    conversation_id: UUID,
    body: MessageCreate,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Send a message (1-2000 characters, at most 20 a minute)."""
    c = await ms.open_conversation(db, conversation_id, str(user["id"]))
    return await ms.send(db, c, user, body.body)


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


@router.delete("/{message_id}", status_code=204)
async def delete_message(
    message_id: UUID,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Delete my own message: it stays in the thread as "Message deleted", without its text."""
    row = (
        await db.execute(
            text("select sender_id, deleted_at from public.messages where id = :id"),
            {"id": str(message_id)},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Message not found")
    if str(row["sender_id"]) != str(user["id"]):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You can only delete your own messages")
    if row["deleted_at"] is None:
        await db.execute(
            text("update public.messages set deleted_at = now(), body = '' where id = :id"),
            {"id": str(message_id)},
        )
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
