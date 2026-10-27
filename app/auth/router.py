"""Auth: password, magic link, Google OAuth, sign-out, session."""

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import service
from app.auth.schemas import AuthResponse, LoginRequest, MagicLinkRequest, SignupRequest, UserOut
from app.core.config import settings
from app.core.database import get_db
from app.core.deps import require_user

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
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)):
    """Email + password sign-in."""
    user = await service.authenticate(db, body.email, body.password)
    profile = (
        await db.execute(
            text("select full_name, role, onboarded_at from public.profiles where id = :id"),
            {"id": user.id},
        )
    ).mappings().first()
    return AuthResponse(
        access_token=service.session_token(user),
        user=_user_out({"id": user.id, "email": user.email, **dict(profile or {})}),
    )


@router.post("/magic-link", status_code=202)
async def magic_link(body: MagicLinkRequest, db: AsyncSession = Depends(get_db)):
    """Email a single-use sign-in link. Always 202 — never reveals whether the account exists."""
    await service.send_magic_link(db, body.email, service.safe_next(body.next))
    return {"sent": True}


@router.get("/confirm")
async def confirm(token: str = Query(min_length=10), next: str | None = None, db: AsyncSession = Depends(get_db)):
    """Consume a magic-link token, set the session cookie and land the user back in the app."""
    user = await service.consume_token(db, token, ("magic_link", "verify_email"))
    if not user:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That link has expired or was already used")
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
    """Google's redirect: exchange the code, upsert the account, set the cookie."""
    if error or not code or not state:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Google sign-in was cancelled")
    next_path = service.read_google_state(state)
    user = await service.google_user(db, code)
    response = RedirectResponse(settings.FRONTEND_URL + next_path)
    _set_cookie(response, service.session_token(user))
    return response


@router.post("/signout")
async def signout():
    """Clear the session cookie."""
    response = JSONResponse({"ok": True})
    response.delete_cookie(settings.SESSION_COOKIE, path="/")
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
