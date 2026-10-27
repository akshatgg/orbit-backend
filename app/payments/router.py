"""Payments routes: checkout, success return, Stripe Connect and the webhook."""

from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import require_user
from app.organisers.service import org_role

from . import service
from .schemas import ConnectIn

router = APIRouter(tags=["payments"])
payments_router = APIRouter(prefix="/payments", tags=["payments"])
me_router = APIRouter(prefix="/me/tickets", tags=["payments"])
checkout_router = APIRouter(prefix="/checkout", tags=["payments"])


@payments_router.post("/registrations/{registration_id}/checkout")
async def start_checkout(
    registration_id: UUID,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Open a Stripe Checkout session for the caller's unpaid registration."""
    return await service.start_checkout(db, registration_id, user)


@me_router.post("/{registration_id}/checkout")
async def start_ticket_checkout(
    registration_id: UUID,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Same checkout, addressed from the ticket in the caller's wallet."""
    return await service.start_checkout(db, registration_id, user)


@checkout_router.get("/success")
async def checkout_success(
    session_id: str = Query(min_length=1),
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Confirm a returned checkout and mark a paid registration as going."""
    return await service.checkout_success(db, session_id, user)


@payments_router.get("/connect/status")
async def connect_status(
    organiser_id: UUID = Query(),
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Whether the organiser has a Stripe account and can take charges."""
    await org_role(db, organiser_id, user, owner=True)
    return await service.connect_status(db, organiser_id)


@payments_router.post("/connect/onboarding")
async def connect_onboarding(
    body: ConnectIn,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Create an Express account if needed and return a fresh onboarding link."""
    await org_role(db, body.organiser_id, user, owner=True)
    return await service.connect_onboarding(db, body.organiser_id)


@payments_router.get("/connect/refresh")
async def connect_refresh(
    organiser_id: UUID = Query(),
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """A new onboarding link for an account that has not finished setup."""
    await org_role(db, organiser_id, user, owner=True)
    return await service.connect_refresh(db, organiser_id)


@payments_router.post("/connect/dashboard")
async def connect_dashboard(
    body: ConnectIn,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """A Stripe Express dashboard login link."""
    await org_role(db, body.organiser_id, user, owner=True)
    return await service.connect_dashboard(db, body.organiser_id)


@payments_router.post("/webhook")
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(default="", alias="Stripe-Signature"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Stripe's webhook; signature-verified and idempotent per event id."""
    payload = await request.body()
    return await service.handle_webhook(db, payload, stripe_signature)


router.include_router(payments_router)
router.include_router(me_router)
router.include_router(checkout_router)
