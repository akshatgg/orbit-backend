"""Sign-in: password, magic link and Google OAuth."""

import logging
from typing import Any, Optional
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.groups.service import join_auto_groups
from app.core.security import (
    create_access_token,
    hash_password,
    hash_token,
    new_opaque_token,
    verify_password,
)
from app.models import EmailToken, Profile, User
from app.notifications.service import send_email

logger = logging.getLogger(__name__)

GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO = "https://openidconnect.googleapis.com/v1/userinfo"

DEFAULT_CATEGORY = "community"


def normalize_email(email: str) -> str:
    return email.strip().lower()


async def get_or_create_user(
    db: AsyncSession, email: str, *, verified: bool = False, full_name: str | None = None
) -> User:
    """Create `users` + `profiles` together; the pair is the account. A new account joins
    every `auto_join` community."""
    email = normalize_email(email)
    values: dict[str, Any] = {"email": email}
    if verified:
        values["email_verified_at"] = text("now()")
    created = (
        await db.execute(
            pg_insert(User).values(**values).on_conflict_do_nothing(index_elements=["email"]).returning(User.id)
        )
    ).first()
    user = (await db.execute(select(User).where(User.email == email))).scalar_one()

    await db.execute(
        pg_insert(Profile)
        .values(id=user.id, email=email, full_name=full_name)
        .on_conflict_do_nothing(index_elements=["id"])
    )
    if created:
        await join_auto_groups(db, user.id)
    if settings.SUPER_ADMIN_EMAIL and email == normalize_email(settings.SUPER_ADMIN_EMAIL):
        await db.execute(
            text("update public.profiles set role = 'super_admin' where id = :id and role <> 'super_admin'"),
            {"id": user.id},
        )
    return user


async def authenticate(db: AsyncSession, email: str, password: str) -> User:
    user = (
        await db.execute(select(User).where(User.email == normalize_email(email)))
    ).scalar_one_or_none()
    if not user or not user.password_hash or not verify_password(password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong email or password")
    return user


async def set_password(db: AsyncSession, user: User, password: str) -> None:
    user.password_hash = hash_password(password)
    await db.flush()


async def issue_magic_link(db: AsyncSession, user: User, kind: str = "magic_link") -> str:
    raw = new_opaque_token()
    await db.execute(
        pg_insert(EmailToken).values(
            user_id=user.id, kind=kind, token_hash=hash_token(raw), expires_at=text(
                f"now() + interval '{settings.MAGIC_LINK_TTL_MINUTES} minutes'"
            )
        )
    )
    return raw


async def send_magic_link(db: AsyncSession, email: str, next_path: str | None = None) -> None:
    """Always succeeds from the caller's point of view: no account enumeration."""
    user = await get_or_create_user(db, email)
    raw = await issue_magic_link(db, user)
    url = f"{settings.BACKEND_URL}{settings.API_PREFIX}/auth/confirm?token={raw}"
    if next_path:
        url += "&" + urlencode({"next": next_path})
    await send_email(
        to=user.email,
        subject="Your Orbit sign-in link",
        html=(
            "<p>Tap to sign in to Orbit:</p>"
            f'<p><a href="{url}">Sign in to Orbit</a></p>'
            f"<p>This link expires in {settings.MAGIC_LINK_TTL_MINUTES} minutes.</p>"
        ),
    )


async def consume_token(db: AsyncSession, raw: str, kinds: tuple[str, ...]) -> Optional[User]:
    row = (
        await db.execute(
            text(
                """
                update public.email_tokens
                   set used_at = now()
                 where token_hash = :h
                   and used_at is null
                   and expires_at > now()
                   and kind = any(:kinds)
                returning user_id
                """
            ),
            {"h": hash_token(raw), "kinds": list(kinds)},
        )
    ).scalar_one_or_none()
    if not row:
        return None
    user = (await db.execute(select(User).where(User.id == row))).scalar_one_or_none()
    if user and user.email_verified_at is None:
        await db.execute(
            text("update public.users set email_verified_at = now() where id = :id"),
            {"id": user.id},
        )
    return user


def google_state(next_path: str | None = None) -> str:
    return jwt.encode(
        {"next": next_path or "/", "purpose": "google_oauth"},
        settings.JWT_SECRET,
        algorithm=settings.JWT_ALGORITHM,
    )


def read_google_state(state: str) -> str:
    try:
        claims = jwt.decode(state, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid OAuth state")
    if claims.get("purpose") != "google_oauth":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid OAuth state")
    return claims.get("next") or "/"


def google_authorize_url(state: str) -> str:
    query = urlencode(
        {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "redirect_uri": f"{settings.BACKEND_URL}{settings.API_PREFIX}/auth/google/callback",
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "access_type": "offline",
            "prompt": "select_account",
        }
    )
    return f"{GOOGLE_AUTH}?{query}"


async def google_user(db: AsyncSession, code: str) -> User:
    if not settings.google_configured:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Google sign-in is not configured")
    async with httpx.AsyncClient(timeout=20) as client:
        token_res = await client.post(
            GOOGLE_TOKEN,
            data={
                "code": code,
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "redirect_uri": f"{settings.BACKEND_URL}{settings.API_PREFIX}/auth/google/callback",
                "grant_type": "authorization_code",
            },
        )
        if token_res.status_code != 200:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Google rejected that sign-in")
        access = token_res.json().get("access_token")
        info = (await client.get(GOOGLE_USERINFO, headers={"Authorization": f"Bearer {access}"})).json()

    email = info.get("email")
    if not email or not info.get("email_verified", True):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Google did not return a verified email")

    user = await get_or_create_user(db, email, verified=True, full_name=info.get("name"))
    if info.get("sub"):
        await db.execute(
            text("update public.users set google_sub = :sub where id = :id and google_sub is null"),
            {"sub": info["sub"], "id": user.id},
        )
    return user


def session_token(user: User, role: str = "user") -> str:
    return create_access_token(str(user.id), user.email, role)


def safe_next(value: str | None) -> str:
    """Only same-site relative paths: an email link must never become an open redirect."""
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    return value
