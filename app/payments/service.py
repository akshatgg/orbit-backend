"""Stripe Checkout, Connect onboarding and webhook handling.

`stripe.api_key` is set per request, never at import time, and every Stripe call
is guarded by `settings.stripe_configured`. Nothing here logs a payload, a key
or a signature.
"""

from functools import partial
from typing import Any, Optional
from uuid import UUID

import stripe
from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.sql import functions as fn

CHECKOUT_CURRENCY_MIN = 1


async def _one(db: AsyncSession, sql: str, params: dict[str, Any]) -> Optional[dict]:
    row = (await db.execute(text(sql), params)).mappings().first()
    return dict(row) if row else None


async def _val(db: AsyncSession, sql: str, params: dict[str, Any]) -> Any:
    return (await db.execute(text(sql), params)).scalar()


def _require_stripe() -> None:
    """503 when the deployment has no Stripe keys, before anything leaves the app."""
    if not settings.stripe_configured:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Card payments are not configured on this server",
        )
    stripe.api_key = settings.STRIPE_SECRET_KEY


async def _stripe(call: Any, **kwargs: Any) -> Any:
    """Run a synchronous Stripe SDK call off the event loop."""
    try:
        return await run_in_threadpool(partial(call, **kwargs))
    except stripe.error.StripeError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "Stripe could not complete that request"
        ) from exc


def _object_id(value: Any) -> Optional[str]:
    if not value:
        return None
    return value if isinstance(value, str) else getattr(value, "id", None)


def _as_dict(value: Any) -> dict:
    """Stripe objects across SDK versions: dict-like in v11, `.to_dict()` in v16."""
    if not value:
        return {}
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return dict(value)


async def _organiser(db: AsyncSession, organiser_id: UUID | str) -> dict:
    org = await _one(
        db,
        "select id, name, status, stripe_account_id, stripe_charges_enabled"
        " from organisers where id = :o",
        {"o": organiser_id},
    )
    if not org:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Organiser not found")
    return org


async def _account_link(account_id: str) -> str:
    link = await _stripe(
        stripe.AccountLink.create,
        account=account_id,
        type="account_onboarding",
        refresh_url=f"{settings.FRONTEND_URL}/organiser/settings?stripe=refresh",
        return_url=f"{settings.FRONTEND_URL}/organiser/settings?stripe=done",
    )
    return link.url


# ─── ticket checkout ────────────────────────────────────────────────────────


async def start_checkout(
    db: AsyncSession, registration_id: UUID | str, user: dict
) -> dict:
    _require_stripe()
    registration = await _one(
        db,
        """
        select r.id, r.user_id, r.status, r.amount, r.currency, r.payment_status,
               e.id as event_id, e.title, tt.name as ticket_name
          from registrations r
          join events e on e.id = r.event_id
          left join ticket_types tt on tt.id = r.ticket_type_id
         where r.id = :id
        """,
        {"id": registration_id},
    )
    if not registration or str(registration["user_id"]) != str(user["id"]):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Registration not found")
    if registration["status"] != "awaiting_payment":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This registration is not awaiting payment"
        )
    if not await fn.payment_hold_ok(db, registration_id):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The hold on this spot has expired — check whether the event still has room",
        )

    amount = round(float(registration["amount"]) * 100)
    if amount < CHECKOUT_CURRENCY_MIN:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This registration has nothing to pay"
        )

    name = registration["title"]
    if registration["ticket_name"]:
        name = f"{name} — {registration['ticket_name']}"

    params: dict[str, Any] = {
        "mode": "payment",
        "line_items": [
            {
                "quantity": 1,
                "price_data": {
                    "currency": str(registration["currency"]).lower(),
                    "unit_amount": amount,
                    "product_data": {"name": name[:250]},
                },
            }
        ],
        "success_url": settings.FRONTEND_URL
        + "/checkout/success?session_id={CHECKOUT_SESSION_ID}",
        "cancel_url": f"{settings.FRONTEND_URL}/events/{registration['event_id']}",
        "client_reference_id": str(registration["id"]),
        "metadata": {
            "registration_id": str(registration["id"]),
            "event_id": str(registration["event_id"]),
        },
    }
    if user.get("email"):
        params["customer_email"] = user["email"]

    session = await _stripe(stripe.checkout.Session.create, **params)
    if not session.url:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "Stripe did not return a checkout URL"
        )

    await db.execute(
        text(
            "update registrations set stripe_checkout_session_id = :session,"
            " updated_at = now() where id = :id and status = 'awaiting_payment'"
        ),
        {"session": session.id, "id": registration_id},
    )
    return {"checkout_url": session.url}


async def checkout_success(db: AsyncSession, session_id: str, user: dict) -> dict:
    _require_stripe()
    session = await _stripe(stripe.checkout.Session.retrieve, session_id)
    metadata = _as_dict(session.metadata)
    registration_id = metadata.get("registration_id")
    if not registration_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Registration not found")

    owner = await _val(
        db,
        "select user_id from registrations where id = :id",
        {"id": registration_id},
    )
    if not owner or str(owner) != str(user["id"]):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "That ticket is not yours")

    if session.payment_status == "paid":
        await fn.mark_registration_paid(
            db,
            registration_id,
            session.id,
            _object_id(session.payment_intent),
        )
    return {
        "status": session.payment_status or session.status,
        "registration_id": registration_id,
    }


# ─── Stripe Connect ─────────────────────────────────────────────────────────


async def connect_status(db: AsyncSession, organiser_id: UUID | str) -> dict:
    org = await _organiser(db, organiser_id)
    return {
        "connected": bool(org["stripe_account_id"]),
        "charges_enabled": bool(org["stripe_charges_enabled"]),
        "account_id": org["stripe_account_id"],
    }


async def connect_onboarding(db: AsyncSession, organiser_id: UUID | str) -> dict:
    _require_stripe()
    org = await _organiser(db, organiser_id)
    account_id = org["stripe_account_id"]
    if not account_id:
        account = await _stripe(
            stripe.Account.create,
            type="express",
            metadata={"organiser_id": str(organiser_id)},
            capabilities={
                "card_payments": {"requested": True},
                "transfers": {"requested": True},
            },
        )
        account_id = account.id
        await db.execute(
            text(
                "update organisers set stripe_account_id = :account, updated_at = now()"
                " where id = :id"
            ),
            {"account": account_id, "id": organiser_id},
        )
    return {"url": await _account_link(account_id)}


async def connect_refresh(db: AsyncSession, organiser_id: UUID | str) -> dict:
    _require_stripe()
    org = await _organiser(db, organiser_id)
    if not org["stripe_account_id"]:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Start onboarding before refreshing the link"
        )
    return {"url": await _account_link(org["stripe_account_id"])}


async def connect_dashboard(db: AsyncSession, organiser_id: UUID | str) -> dict:
    _require_stripe()
    org = await _organiser(db, organiser_id)
    if not org["stripe_account_id"]:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Start onboarding before opening the dashboard"
        )
    link = await _stripe(
        stripe.Account.create_login_link, account=org["stripe_account_id"]
    )
    return {"url": link.url}


# ─── webhook ────────────────────────────────────────────────────────────────


async def handle_webhook(db: AsyncSession, payload: bytes, signature: str) -> dict:
    _require_stripe()
    if not settings.STRIPE_WEBHOOK_SECRET:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Stripe webhooks are not configured on this server",
        )
    try:
        event = _as_dict(
            await run_in_threadpool(
                stripe.Webhook.construct_event,
                payload,
                signature,
                settings.STRIPE_WEBHOOK_SECRET,
            )
        )
    except (stripe.error.SignatureVerificationError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Invalid Stripe signature"
        ) from exc

    event_type = event["type"]
    first_seen = await _val(
        db,
        """
        insert into stripe_events (id, type, account, livemode)
        values (:id, :type, :account, :livemode)
        on conflict (id) do nothing
        returning id
        """,
        {
            "id": event["id"],
            "type": event_type,
            "account": event.get("account"),
            "livemode": bool(event.get("livemode")),
        },
    )
    if not first_seen:
        return {"received": True}  # replayed delivery: already applied

    obj = event["data"]["object"]
    if event_type == "checkout.session.completed":
        metadata = obj.get("metadata") or {}
        registration_id = metadata.get("registration_id")
        if registration_id:
            try:
                UUID(str(registration_id))
            except ValueError:
                registration_id = None
        if registration_id:
            await fn.mark_registration_paid(
                db,
                registration_id,
                obj.get("id"),
                _object_id(obj.get("payment_intent")),
            )
    elif event_type == "account.updated":
        account_id = obj.get("id")
        if account_id:
            await db.execute(
                text(
                    "update organisers set stripe_charges_enabled = :enabled,"
                    " updated_at = now() where stripe_account_id = :account"
                ),
                {"enabled": bool(obj.get("charges_enabled")), "account": account_id},
            )
    return {"received": True}
