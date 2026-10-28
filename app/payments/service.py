"""Stripe Checkout for paid tickets, Stripe Connect for organisers, and the webhook.

Ported from the frontend's `src/lib/payments/{index,fulfil,connect}.ts`, the
Stripe webhook route and the checkout success page. Every Stripe call goes
through `gateway.get_gateway()`, which is None when the deployment has no
Stripe key; each endpoint has a disabled path for that case.

Errors are `{"detail": "<code>"}` with a stable code (listed per function) so
the frontend can keep its own wording. Nothing here logs a payload, a key or a
signature.
"""

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote
from uuid import UUID

import stripe
from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.sql import functions as fn

from . import gateway, logic
from .gateway import GatewayError, StripeGateway

logger = logging.getLogger(__name__)

SESSION_ID_RE = re.compile(r"^cs_[A-Za-z0-9_]+$")
FULFIL_EVENTS = ("checkout.session.completed", "checkout.session.async_payment_succeeded")


class WebhookFailed(Exception):
    """The webhook claimed an event but could not apply it; the claim was released."""


# ─── helpers ────────────────────────────────────────────────────────────────


async def _one(db: AsyncSession, sql: str, params: dict[str, Any]) -> Optional[dict]:
    row = (await db.execute(text(sql), params)).mappings().first()
    return dict(row) if row else None


async def _val(db: AsyncSession, sql: str, params: dict[str, Any]) -> Any:
    return (await db.execute(text(sql), params)).scalar()


def _fail(code: int, detail: str) -> HTTPException:
    return HTTPException(code, detail)


def _require_gateway() -> StripeGateway:
    """503 when the deployment has no Stripe key, before anything leaves the app."""
    gw = gateway.get_gateway()
    if gw is None:
        raise _fail(status.HTTP_503_SERVICE_UNAVAILABLE, "payments_not_configured")
    return gw


def _stripe_failed(exc: GatewayError) -> HTTPException:
    return _fail(status.HTTP_502_BAD_GATEWAY, exc.code)


def _uuid(value: Any) -> Optional[str]:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError):
        return None


def _object_id(value: Any) -> Optional[str]:
    if not value:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("id")
    return getattr(value, "id", None)


def _success_url(session_id: str = "{CHECKOUT_SESSION_ID}") -> str:
    return f"{settings.FRONTEND_URL}/checkout/success?session_id={session_id}"


def payments_page_url(organiser_id: UUID | str, extra: str = "") -> str:
    """The organiser's payout settings page on the frontend."""
    org = quote(str(organiser_id), safe="")
    return f"{settings.FRONTEND_URL}/organiser/settings/payments?org={org}{extra}"


async def _registration_paid(db: AsyncSession, registration_id: str) -> None:
    """Hook: a registration just became paid (status going).

    Notifications (W3) call
    `app.notifications.service.notify_registration_changed(db, [UUID(registration_id)], reason="paid")`
    here. It must never raise into the payment hand-off. Until then: no-op.
    """
    return None


# ─── ticket checkout ────────────────────────────────────────────────────────


async def start_checkout(db: AsyncSession, registration_id: UUID, user: dict) -> dict:
    """Open (or reuse) a Stripe Checkout session for the caller's unpaid registration.

    503 payments_not_configured · 404 registration_not_found ·
    409 already_paid | already_going | awaiting_approval | waitlisted |
        not_awaiting_payment | free_ticket | unsupported_currency |
        event_not_on_sale | event_ended | no_organiser |
        organiser_not_approved | stripe_not_ready | hold_expired ·
    502 stripe_auth | stripe_unreachable | stripe_busy | stripe_error
    """
    gw = _require_gateway()

    # 1. Ownership and amount come from the database.
    reg = await _one(
        db,
        """
        select r.id, r.user_id, r.event_id, r.status, r.amount, r.currency,
               r.payment_status, r.stripe_checkout_session_id, r.updated_at,
               tt.name as ticket_name,
               e.title, e.status as event_status, e.starts_at, e.ends_at, e.organiser_id
          from registrations r
          join events e on e.id = r.event_id
          left join ticket_types tt on tt.id = r.ticket_type_id
         where r.id = :id
        """,
        {"id": registration_id},
    )
    blocker = logic.checkout_blocker(reg, user["id"])
    if blocker == "registration_not_found" or not reg:
        raise _fail(status.HTTP_404_NOT_FOUND, "registration_not_found")
    if blocker:
        raise _fail(status.HTTP_409_CONFLICT, blocker)

    if reg["event_status"] != "published":
        raise _fail(status.HTTP_409_CONFLICT, "event_not_on_sale")
    if (reg["ends_at"] or reg["starts_at"]) < datetime.now(timezone.utc):
        raise _fail(status.HTTP_409_CONFLICT, "event_ended")
    if not reg["organiser_id"]:
        raise _fail(status.HTTP_409_CONFLICT, "no_organiser")

    try:
        # 2. Reuse an open checkout so a double tap can't create two payable sessions.
        stored = reg["stripe_checkout_session_id"]
        if stored:
            try:
                existing = await gw.retrieve_session(stored)
            except GatewayError:
                existing = None
            if existing and logic.is_reusable_session(existing):
                return {"checkout_url": existing["url"]}
            if existing and existing.get("payment_status") == "paid":
                await fulfil_checkout_session(db, existing)
                return {"checkout_url": _success_url(existing["id"])}

        # 3. The organiser must be able to take money.
        org = await _one(
            db,
            "select id, status, stripe_account_id, stripe_charges_enabled"
            " from organisers where id = :o",
            {"o": reg["organiser_id"]},
        )
        if not org or org["status"] != "approved":
            raise _fail(status.HTTP_409_CONFLICT, "organiser_not_approved")
        if not logic.is_organiser_payable(org):
            raise _fail(status.HTTP_409_CONFLICT, "stripe_not_ready")

        # 4. The 30-minute hold must still be live, or there must still be room.
        if not await fn.payment_hold_ok(db, reg["id"]):
            raise _fail(status.HTTP_409_CONFLICT, "hold_expired")

        amount = logic.to_pence(reg["amount"])
        fee = logic.application_fee_pence(amount, settings.platform_fee_percent)
        name = logic.line_item_name(reg["title"], reg["ticket_name"])
        metadata = {"registration_id": str(reg["id"]), "event_id": str(reg["event_id"])}
        payment_intent_data: dict[str, Any] = {
            "description": name,
            "metadata": metadata,
            "transfer_data": {"destination": org["stripe_account_id"]},
        }
        if fee > 0:
            payment_intent_data["application_fee_amount"] = fee
        params: dict[str, Any] = {
            "mode": "payment",
            "line_items": [
                {
                    "quantity": 1,
                    "price_data": {
                        "currency": logic.SUPPORTED_CURRENCY,
                        "unit_amount": amount,
                        "product_data": {"name": name},
                    },
                }
            ],
            "client_reference_id": str(reg["id"]),
            "metadata": metadata,
            "payment_intent_data": payment_intent_data,
            "expires_at": logic.checkout_expires_at(),
            "locale": "en-GB",
            "success_url": _success_url(),
            "cancel_url": f"{settings.FRONTEND_URL}/events/{reg['event_id']}",
        }
        if user.get("email"):
            params["customer_email"] = user["email"]

        # Concurrent taps on the same registration state get the same session back.
        key = f"orbit-checkout-{reg['id']}-{stored or 'new'}-{reg['updated_at'].isoformat()}"
        session = await gw.create_session(params, key)
    except GatewayError as exc:
        raise _stripe_failed(exc) from exc
    if not session.get("url"):
        logger.error("[payments] checkout session %s has no URL", session.get("id"))
        raise _fail(status.HTTP_502_BAD_GATEWAY, "stripe_error")

    # 5. Remember the session on the registration (this also refreshes the hold).
    await db.execute(
        text(
            "update registrations set stripe_checkout_session_id = :session,"
            " updated_at = now() where id = :id and status = 'awaiting_payment'"
        ),
        {"session": session["id"], "id": reg["id"]},
    )
    return {"checkout_url": session["url"]}


# ─── fulfilment (webhook and success page) ──────────────────────────────────


async def fulfil_checkout_session(db: AsyncSession, session: dict) -> str:
    """Mark the session's registration paid (status going) when Stripe says it is paid.

    Returns "marked", "already" or "skipped". Safe to call any number of times:
    the row is locked, so the webhook and the success page cannot both mark it.
    """
    if not logic.should_fulfil(session):
        return "skipped"
    registration_id = _uuid(session["metadata"]["registration_id"])
    if not registration_id:
        logger.warning("[payments] paid session %s has a malformed registration id", session.get("id"))
        return "skipped"

    current = await _one(
        db,
        "select id, status, payment_status, stripe_checkout_session_id"
        " from registrations where id = :id for update",
        {"id": registration_id},
    )
    if not current:
        logger.warning(
            "[payments] paid session %s for a registration that no longer exists (%s)",
            session.get("id"),
            registration_id,
        )
        return "skipped"
    if current["payment_status"] == "paid":
        stored = current["stripe_checkout_session_id"]
        if stored and stored != session.get("id"):
            # Two different checkouts were paid for one ticket: refund one in Stripe.
            logger.warning(
                "[payments] possible duplicate payment: registration %s paid by %s, extra %s",
                registration_id,
                stored,
                session.get("id"),
            )
        return "already"

    after = await fn.mark_registration_paid(
        db, registration_id, session["id"], _object_id(session.get("payment_intent"))
    )
    if not after or after.get("payment_status") != "paid":
        # mark_registration_paid only moves awaiting_payment / cancelled / waitlisted rows.
        logger.warning(
            "[payments] paid session %s didn't update registration %s (status %s)",
            session.get("id"),
            registration_id,
            current["status"],
        )
        return "skipped"
    await _registration_paid(db, registration_id)
    return "marked"


async def checkout_success(db: AsyncSession, session_id: str, user: dict) -> dict:
    """What the buyer sees after Stripe sends them back; confirms a paid ticket now.

    503 payments_not_configured · 403 not_your_ticket. An unknown or malformed
    session is not an error: it returns outcome "unknown".
    """
    gw = _require_gateway()
    body: dict[str, Any] = {
        "outcome": "unknown",
        "status": None,
        "registration_id": None,
        "event": None,
        "ticket_type": None,
        "amount_total": None,
        "currency": None,
        "buyer_email": None,
        "checkout_url": None,
    }

    session: Optional[dict] = None
    if SESSION_ID_RE.match(session_id):
        try:
            session = await gw.retrieve_session(session_id)
        except GatewayError:
            session = None
    if not session:
        return body

    outcome = logic.checkout_outcome(session)
    registration_id = _uuid((session.get("metadata") or {}).get("registration_id"))
    owner = None
    if registration_id:
        owner = await _val(
            db, "select user_id from registrations where id = :id", {"id": registration_id}
        )
        if owner and str(owner) != str(user["id"]):
            raise _fail(status.HTTP_403_FORBIDDEN, "not_your_ticket")

    # Don't make people wait for the webhook: confirm the ticket now (idempotent).
    if outcome == "paid" and owner:
        try:
            async with db.begin_nested():
                await fulfil_checkout_session(db, session)
        except Exception:
            logger.exception("[payments] fulfil on the success page failed for %s", session.get("id"))

    reg = None
    if owner:
        reg = await _one(
            db,
            """
            select r.id, r.status, tt.name as ticket_name,
                   e.id as event_id, e.title, e.starts_at, e.venue_name, e.format
              from registrations r
              join events e on e.id = r.event_id
              left join ticket_types tt on tt.id = r.ticket_type_id
             where r.id = :id
            """,
            {"id": registration_id},
        )

    details = session.get("customer_details") or {}
    currency = session.get("currency")
    body.update(
        outcome=outcome,
        amount_total=session.get("amount_total"),
        currency=currency.upper() if currency else None,
        buyer_email=details.get("email") or session.get("customer_email"),
        checkout_url=session.get("url") if outcome == "open" else None,
    )
    if reg:
        body.update(
            status=reg["status"],
            registration_id=str(reg["id"]),
            event={
                "id": str(reg["event_id"]),
                "title": reg["title"],
                "starts_at": reg["starts_at"],
                "venue_name": reg["venue_name"],
                "format": reg["format"],
            },
            ticket_type={"name": reg["ticket_name"]} if reg["ticket_name"] else None,
        )
    return body


# ─── Stripe Connect ─────────────────────────────────────────────────────────


async def _organiser(db: AsyncSession, organiser_id: UUID | str) -> dict:
    org = await _one(
        db,
        "select id, name, slug, status, bio, website, contact_email,"
        " stripe_account_id, stripe_charges_enabled from organisers where id = :o",
        {"o": organiser_id},
    )
    if not org:
        raise _fail(status.HTTP_404_NOT_FOUND, "organiser_not_found")
    return org


async def sync_account_status(db: AsyncSession, account: dict) -> None:
    """Copy `charges_enabled` from a Stripe account onto every organiser using it."""
    account_id = account.get("id")
    if not account_id:
        return
    enabled = bool(account.get("charges_enabled"))
    await db.execute(
        text(
            "update organisers set stripe_charges_enabled = :enabled, updated_at = now()"
            " where stripe_account_id = :account and stripe_charges_enabled <> :enabled"
        ),
        {"enabled": enabled, "account": account_id},
    )


def _summarise_account(account: dict) -> dict:
    requirements = account.get("requirements") or {}
    return {
        "charges_enabled": bool(account.get("charges_enabled")),
        "payouts_enabled": bool(account.get("payouts_enabled")),
        "details_submitted": bool(account.get("details_submitted")),
        "due_count": len(requirements.get("currently_due") or []),
        "disabled_reason": requirements.get("disabled_reason"),
    }


async def connect_status(db: AsyncSession, organiser_id: UUID | str) -> dict:
    """The organiser's payout state, read live from Stripe and synced to our copy.

    Never 503: without Stripe keys the state is "unconfigured". When Stripe
    cannot be reached, `live` is false and the stored flag is used.
    """
    org = await _organiser(db, organiser_id)
    gw = gateway.get_gateway()
    account_id = org["stripe_account_id"]
    summary: Optional[dict] = None
    if gw is not None and account_id:
        try:
            account = await gw.retrieve_account(account_id)
            await sync_account_status(db, account)
            summary = _summarise_account(account)
        except GatewayError:
            summary = None

    charges_enabled = summary["charges_enabled"] if summary else bool(org["stripe_charges_enabled"])
    return {
        "configured": gw is not None,
        "state": logic.connect_state(
            configured=gw is not None, account_id=account_id, charges_enabled=charges_enabled
        ),
        "connected": bool(account_id),
        "account_id": account_id,
        "charges_enabled": charges_enabled,
        "payouts_enabled": summary["payouts_enabled"] if summary else None,
        "details_submitted": summary["details_submitted"] if summary else None,
        "due_count": summary["due_count"] if summary else None,
        "disabled_reason": summary["disabled_reason"] if summary else None,
        "live": summary is not None,
    }


def _http_url(value: Optional[str]) -> Optional[str]:
    if value and re.match(r"^https?://[^\s]+$", value.strip(), re.IGNORECASE):
        return value.strip()
    return None


async def _ensure_connected_account(
    db: AsyncSession, gw: StripeGateway, org: dict, fallback_email: Optional[str]
) -> str:
    """The organiser's connected account id, creating a GB Express account the first time."""
    if org["stripe_account_id"]:
        return org["stripe_account_id"]

    business_profile: dict[str, Any] = {
        "name": org["name"],
        "product_description": (org["bio"] or "")[:500]
        or f"Event tickets sold on ORBIT by {org['name']}",
    }
    if _http_url(org["website"]):
        business_profile["url"] = _http_url(org["website"])
    if org["contact_email"]:
        business_profile["support_email"] = org["contact_email"]
    params: dict[str, Any] = {
        "type": "express",
        "country": "GB",
        "default_currency": "gbp",
        "business_profile": business_profile,
        "capabilities": {
            "card_payments": {"requested": True},
            "transfers": {"requested": True},
        },
        "metadata": {"organiser_id": str(org["id"]), "organiser_slug": org["slug"]},
    }
    email = org["contact_email"] or fallback_email
    if email:
        params["email"] = email

    # A double click must not create two accounts.
    account = await gw.create_account(params, f"orbit-connect-account-{org['id']}")
    stored = await _val(
        db,
        """
        update organisers
           set stripe_account_id = :account, stripe_charges_enabled = :enabled,
               updated_at = now()
         where id = :id and stripe_account_id is null
        returning stripe_account_id
        """,
        {
            "account": account["id"],
            "enabled": bool(account.get("charges_enabled")),
            "id": org["id"],
        },
    )
    if not stored:
        # Someone else stored an account first: use theirs.
        stored = await _val(
            db, "select stripe_account_id from organisers where id = :id", {"id": org["id"]}
        )
    # Keep the account id even if creating the onboarding link fails next.
    await db.commit()
    return stored


async def _onboarding_link(gw: StripeGateway, organiser_id: UUID | str, account_id: str) -> str:
    link = await gw.create_account_link(
        {
            "account": account_id,
            "type": "account_onboarding",
            "refresh_url": payments_page_url(organiser_id, "&stripe=refresh"),
            "return_url": payments_page_url(organiser_id, "&stripe=return"),
        }
    )
    return link["url"]


async def connect_onboarding(db: AsyncSession, organiser_id: UUID | str, user: dict) -> dict:
    """Create the Express account if needed and return a fresh onboarding link.

    503 payments_not_configured · 404 organiser_not_found ·
    403 organiser_not_approved · 502 stripe_*
    """
    gw = _require_gateway()
    org = await _organiser(db, organiser_id)
    if org["status"] != "approved":
        raise _fail(status.HTTP_403_FORBIDDEN, "organiser_not_approved")
    try:
        account_id = await _ensure_connected_account(db, gw, org, user.get("email"))
        return {"url": await _onboarding_link(gw, org["id"], account_id)}
    except GatewayError as exc:
        raise _stripe_failed(exc) from exc


async def connect_refresh_location(db: AsyncSession, organiser_id: UUID | str) -> str:
    """Where to send an organiser whose onboarding link expired or was used.

    A fresh Stripe link for an existing account; otherwise back to the payments
    page (with `&error=link` when Stripe failed). Never creates an account.
    """
    org = await _organiser(db, organiser_id)
    gw = gateway.get_gateway()
    if gw is None or not org["stripe_account_id"]:
        return payments_page_url(org["id"])
    try:
        return await _onboarding_link(gw, org["id"], org["stripe_account_id"])
    except GatewayError:
        return payments_page_url(org["id"], "&error=link")


async def connect_dashboard(db: AsyncSession, organiser_id: UUID | str) -> dict:
    """A single-use Stripe Express dashboard login link.

    503 payments_not_configured · 404 organiser_not_found ·
    409 not_connected · 502 stripe_*
    """
    gw = _require_gateway()
    org = await _organiser(db, organiser_id)
    if not org["stripe_account_id"]:
        raise _fail(status.HTTP_409_CONFLICT, "not_connected")
    try:
        link = await gw.create_login_link(org["stripe_account_id"])
    except GatewayError as exc:
        raise _stripe_failed(exc) from exc
    return {"url": link["url"]}


# ─── webhook ────────────────────────────────────────────────────────────────


def _verified_event(payload: bytes, signature: str) -> Optional[dict]:
    """The event if any configured secret verifies the signature, else None."""
    try:
        body = payload.decode("utf-8")
    except UnicodeDecodeError:
        return None
    for secret in settings.stripe_webhook_secrets:
        try:
            stripe.WebhookSignature.verify_header(
                body, signature, secret, stripe.Webhook.DEFAULT_TOLERANCE
            )
        except stripe.SignatureVerificationError:
            continue  # try the next secret
        try:
            event = json.loads(body)
        except ValueError:
            return None
        return event if isinstance(event, dict) and event.get("id") else None
    return None


async def _apply_event(db: AsyncSession, event: dict) -> None:
    obj = (event.get("data") or {}).get("object") or {}
    if event.get("type") in FULFIL_EVENTS:
        await fulfil_checkout_session(db, obj)
    elif event.get("type") == "account.updated":
        await sync_account_status(db, obj)
    # Anything else is not ours.


async def handle_webhook(db: AsyncSession, payload: bytes, signature: str) -> dict:
    """Stripe → Orbit. Verified against every configured signing secret.

    503 payments_not_configured · 400 missing_signature | invalid_signature.
    Each event id is claimed in `stripe_events` first; a replay returns
    `{"received": true, "duplicate": true}`. If applying the event fails, the
    claim is deleted and `WebhookFailed` is raised (the router answers 500) so
    Stripe's retry is processed.
    """
    if gateway.get_gateway() is None or not settings.stripe_webhook_secrets:
        raise _fail(status.HTTP_503_SERVICE_UNAVAILABLE, "payments_not_configured")
    if not signature:
        raise _fail(status.HTTP_400_BAD_REQUEST, "missing_signature")
    event = _verified_event(payload, signature)
    if event is None:
        raise _fail(status.HTTP_400_BAD_REQUEST, "invalid_signature")

    claimed = await _val(
        db,
        """
        insert into stripe_events (id, type, account, livemode)
        values (:id, :type, :account, :livemode)
        on conflict (id) do nothing
        returning id
        """,
        {
            "id": event["id"],
            "type": str(event.get("type") or ""),
            "account": event.get("account"),
            "livemode": bool(event.get("livemode")),
        },
    )
    if not claimed:
        return {"received": True, "duplicate": True}

    try:
        async with db.begin_nested():
            await _apply_event(db, event)
    except Exception as exc:
        logger.exception("[stripe webhook] %s %s failed", event.get("type"), event["id"])
        # Release the claim so Stripe's retry gets processed.
        await db.execute(text("delete from stripe_events where id = :id"), {"id": event["id"]})
        raise WebhookFailed(event["id"]) from exc
    return {"received": True}
