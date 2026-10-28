"""Pure payment rules: no Stripe and no database.

A port of the frontend's `src/lib/payments/logic.ts`, so checkout behaves the
same before and after the cutover. Where the frontend returned English copy,
these return a stable code the frontend maps to its own wording.
"""

import math
import time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Literal, Optional

# Paid spots are held for 30 minutes (taken_spots / payment_hold_ok in SQL).
HOLD_MINUTES = 30
# Stripe needs a session to live at least 30 minutes by its own clock, so add a
# minute of slack. Saving the session id refreshes the hold, so both end together.
CHECKOUT_MINUTES = HOLD_MINUTES + 1
# A stored session is reused only while it has this much life left.
REUSE_MARGIN_SECONDS = 2 * 60

SUPPORTED_CURRENCY = "gbp"
PRODUCT_NAME_MAX = 250

ConnectState = Literal["unconfigured", "not_connected", "incomplete", "connected"]
CheckoutOutcome = Literal["paid", "processing", "open", "expired"]


def to_pence(amount: Any) -> int:
    """£12.50 → 1250. Accepts numbers, Decimals or numeric strings. Never negative."""
    if amount is None:
        return 0
    try:
        value = Decimal(str(amount).strip())
    except (InvalidOperation, ValueError):
        return 0
    if not value.is_finite() or value <= 0:
        return 0
    return int((value * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def application_fee_pence(amount_pence: int, percent: float) -> int:
    """Orbit's cut in pence, rounded to the nearest penny, never more than the price."""
    if amount_pence <= 0 or percent <= 0:
        return 0
    fee = int(Decimal(str(amount_pence * percent / 100)).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return min(amount_pence, fee)


def line_item_name(event_title: str, ticket_name: Optional[str] = None) -> str:
    """"Founders Friday — Student". Stripe caps product names, so trim to 250."""
    title = (event_title or "").strip()
    ticket = (ticket_name or "").strip()
    name = f"{title} — {ticket}" if ticket else title
    if len(name) > PRODUCT_NAME_MAX:
        return name[: PRODUCT_NAME_MAX - 1] + "…"
    return name


def checkout_expires_at(now: Optional[float] = None) -> int:
    """Unix seconds at which a Checkout Session created now should expire."""
    now = time.time() if now is None else now
    return math.floor(now) + CHECKOUT_MINUTES * 60


def is_reusable_session(session: dict, now: Optional[float] = None) -> bool:
    """True while a stored session is open, has a URL and a couple of minutes left."""
    now = time.time() if now is None else now
    expires_at = session.get("expires_at") or 0
    return (
        session.get("status") == "open"
        and bool(session.get("url"))
        and expires_at - now > REUSE_MARGIN_SECONDS
    )


def checkout_blocker(registration: Optional[dict], user_id: str) -> Optional[str]:
    """Why a registration cannot go to checkout (a code), or None when it can."""
    if not registration or str(registration["user_id"]) != str(user_id):
        return "registration_not_found"
    if registration["payment_status"] == "paid":
        return "already_paid"
    status = registration["status"]
    if status != "awaiting_payment":
        return {
            "going": "already_going",
            "pending": "awaiting_approval",
            "waitlisted": "waitlisted",
        }.get(status, "not_awaiting_payment")
    if to_pence(registration["amount"]) <= 0:
        return "free_ticket"
    if str(registration["currency"]).lower() != SUPPORTED_CURRENCY:
        return "unsupported_currency"
    return None


def connect_state(
    *, configured: bool, account_id: Optional[str], charges_enabled: Optional[bool]
) -> ConnectState:
    """Where an organiser is with Stripe payouts."""
    if not configured:
        return "unconfigured"
    if not account_id:
        return "not_connected"
    return "connected" if charges_enabled else "incomplete"


def is_organiser_payable(org: Optional[dict]) -> bool:
    """Can this organiser take money right now?"""
    return bool(org and org.get("stripe_account_id") and org.get("stripe_charges_enabled"))


def checkout_outcome(session: dict) -> CheckoutOutcome:
    """What a Checkout Session's status/payment_status means for the buyer."""
    if session.get("payment_status") in ("paid", "no_payment_required"):
        return "open" if session.get("status") == "open" else "paid"
    if session.get("status") == "complete":
        return "processing"  # e.g. a bank payment still clearing
    if session.get("status") == "expired":
        return "expired"
    return "open"


def should_fulfil(session: dict) -> bool:
    """Should a checkout.session.* event mark the registration as paid?"""
    metadata = session.get("metadata") or {}
    return session.get("payment_status") == "paid" and bool(metadata.get("registration_id"))
