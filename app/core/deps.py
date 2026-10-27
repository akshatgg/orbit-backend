"""Authentication and authorisation dependencies.

Orbit's Supabase RLS policies are replaced by these dependencies: every route
that used to rely on `auth.uid()` now declares who may call it. The database
still receives the acting user (so `auth.uid()` inside the ported functions
answers correctly), but the decision is made here — one place to read.
"""

from typing import Optional

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db, set_actor
from app.core.security import decode_token


def _token_from(request: Request) -> Optional[str]:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return request.cookies.get(settings.SESSION_COOKIE)


async def current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Optional[dict]:
    """The signed-in user, or None. Never raises — public routes use this."""
    claims = decode_token(_token_from(request) or "")
    if not claims:
        return None
    rows = await db.execute(
        text(
            """
            select p.id, p.email, p.full_name, p.avatar_url, p.role,
                   p.onboarded_at, p.university, u.email_verified_at
              from public.profiles p
              join public.users u on u.id = p.id
             where p.id = :id
            """
        ),
        {"id": claims["sub"]},
    )
    row = rows.mappings().first()
    if not row:
        return None
    await set_actor(db, str(row["id"]))
    return dict(row)


async def require_user(user: Optional[dict] = Depends(current_user)) -> dict:
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in to continue")
    return user


async def require_admin(user: dict = Depends(require_user)) -> dict:
    if user["role"] not in ("admin", "super_admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admins only")
    return user


async def require_super_admin(user: dict = Depends(require_user)) -> dict:
    if user["role"] != "super_admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Super admins only")
    return user


async def require_org_member(
    organiser_id: str,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await _org_role(db, organiser_id, user, owner=False)


async def require_org_owner(
    organiser_id: str,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await _org_role(db, organiser_id, user, owner=True)


async def _org_role(db: AsyncSession, organiser_id: str, user: dict, *, owner: bool) -> dict:
    if user["role"] in ("admin", "super_admin"):
        return {**user, "org_role": "admin"}
    rows = await db.execute(
        text(
            "select role from public.organiser_members"
            " where organiser_id = :org and user_id = :uid"
        ),
        {"org": organiser_id, "uid": user["id"]},
    )
    role = rows.scalar_one_or_none()
    if not role:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not a member of this organiser")
    if owner and role != "owner":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the owner can do that")
    return {**user, "org_role": role}


async def require_event_manager(
    event_id: str,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if user["role"] in ("admin", "super_admin"):
        return user
    rows = await db.execute(
        text("select public.can_manage_event(:ev)"), {"ev": event_id}
    )
    if not rows.scalar():
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You cannot manage this event")
    return user


async def require_cron(authorization: str = Header(default="")) -> None:
    expected = f"Bearer {settings.CRON_SECRET}"
    if not settings.CRON_SECRET or authorization != expected:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unauthorized")
