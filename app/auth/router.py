"""Auth: password, magic link, Google OAuth, sign-out, session."""

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import service
from app.auth.schemas import AuthResponse, LoginRequest, MagicLinkRequest, SignupRequest, UserOut
from app.core.config import settings
from app.core.database import get_db
from app.core.deps import require_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


def _user_out(row: dict) -> UserOut:
    return UserOut(
        id=str(row["id"]),
        email=row.get("email") or "",
        full_name=row.get("full_name"),
        role=row.get("role") or "user",
        onboarded=row.get("onboarded_at") is not None,
    )


def _set_cookie(response, token: str) -> None:
    response.set_cookie(
        settings.SESSION_COOKIE,
        token,
        max_age=settings.JWT_EXPIRE_HOURS * 3600,
        httponly=True,
        secure=not settings.is_local,
        samesite="lax",
        path="/",
        domain=settings.SESSION_COOKIE_DOMAIN or None,
    )


def _clear_cookie(response) -> None:
    """Expire the session cookie; with a shared domain, also the old host-only one."""
    response.delete_cookie(
        settings.SESSION_COOKIE, path="/", domain=settings.SESSION_COOKIE_DOMAIN or None
    )
    if settings.SESSION_COOKIE_DOMAIN:
        response.delete_cookie(settings.SESSION_COOKIE, path="/")


def _login_error(kind: str) -> RedirectResponse:
    """Send a failed sign-in back to the login page, which explains `?error=`."""
    return RedirectResponse(
        f"{settings.FRONTEND_URL}/login?error={kind}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/signup", response_model=None, status_code=201)
async def signup(body: SignupRequest, db: AsyncSession = Depends(get_db)):
    """Create an account: with a password it signs in, without one it emails a magic link."""
    user = await service.get_or_create_user(db, body.email, full_name=body.full_name)
    if body.password:
        await service.set_password(db, user, body.password)
        token = service.session_token(user)
        return AuthResponse(access_token=token, user=_user_out(
            {"id": user.id, "email": user.email, "full_name": body.full_name}
        ))
    await service.send_magic_link(db, body.email)
    return {"sent": True}


@router.post("/login", response_model=AuthResponse)
async def login(body: LoginRequest, response: Response, db: AsyncSession = Depends(get_db)):
    """Email + password sign-in. Sets the session cookie and returns the token."""
    user = await service.authenticate(db, body.email, body.password)
    profile = (
        await db.execute(
            text("select full_name, role, onboarded_at from public.profiles where id = :id"),
            {"id": user.id},
        )
    ).mappings().first()
    token = service.session_token(user)
    _set_cookie(response, token)
    return AuthResponse(
        access_token=token,
        user=_user_out({"id": user.id, "email": user.email, **dict(profile or {})}),
    )


@router.post("/magic-link", status_code=202)
async def magic_link(body: MagicLinkRequest, db: AsyncSession = Depends(get_db)):
    """Email a single-use sign-in link. Always 202 — never reveals whether the account exists."""
    await service.send_magic_link(db, body.email, service.safe_next(body.next))
    return {"sent": True}


@router.get("/confirm")
async def confirm(token: str = "", next: str | None = None, db: AsyncSession = Depends(get_db)):
    """Consume a magic-link token, set the session cookie and land the user back in the app.

    An expired, used or malformed link goes to `/login?error=link`.
    """
    user = await service.consume_token(db, token, ("magic_link", "verify_email")) if token else None
    if not user:
        return _login_error("link")
    response = RedirectResponse(settings.FRONTEND_URL + service.safe_next(next))
    _set_cookie(response, service.session_token(user))
    return response


@router.get("/google/start")
async def google_start(next: str | None = None):
    """Begin Google sign-in."""
    if not settings.google_configured:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Google sign-in is not configured")
    return RedirectResponse(service.google_authorize_url(service.google_state(service.safe_next(next))))


@router.get("/google/callback")
async def google_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Google's redirect: exchange the code, upsert the account, set the cookie.

    A cancelled, forged or failed sign-in goes to `/login?error=auth`.
    """
    if error or not code or not state:
        return _login_error("auth")
    try:
        next_path = service.read_google_state(state)
        user = await service.google_user(db, code)
    except HTTPException as exc:
        logger.info("Google sign-in refused: %s", exc.detail)
        return _login_error("auth")
    except (httpx.HTTPError, ValueError):
        logger.warning("Google sign-in failed", exc_info=True)
        return _login_error("auth")
    response = RedirectResponse(settings.FRONTEND_URL + next_path)
    _set_cookie(response, service.session_token(user))
    return response


@router.post("/signout")
async def signout():
    """Clear the session cookie."""
    response = JSONResponse({"ok": True})
    _clear_cookie(response)
    return response


@router.get("/session", response_model=UserOut)
async def session(user: dict = Depends(require_user)):
    """Who the caller is, from the cookie or bearer token."""
    return _user_out(user)


@router.post("/password")
async def set_password(
    request: Request,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Set or change the caller's password. Body: `{password}`."""
    body = await request.json()
    password = str(body.get("password") or "")
    if len(password) < 8:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Use at least 8 characters")
    await db.execute(
        text("update public.users set password_hash = :h, updated_at = now() where id = :id"),
        {"h": service.hash_password(password), "id": user["id"]},
    )
    return {"ok": True}
