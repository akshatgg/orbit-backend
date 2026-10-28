"""The Stripe call layer: the only code in Orbit that talks to Stripe's API.

`get_gateway()` returns None when the deployment has no Stripe key, so every
caller has a disabled path. Every method returns plain dicts (never Stripe
objects) and turns Stripe failures into `GatewayError`, whose `code` mirrors the
frontend's `describeStripeError`. The service always looks the gateway up
through this module at call time, so it can be swapped at this boundary.

Nothing here logs a key, a payload or a signature.
"""

import logging
from typing import Any, Optional

import stripe

from app.core.config import settings

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 20
NETWORK_RETRIES = 2


class GatewayError(Exception):
    """A Stripe call failed. `code` is safe to show; details go to the log."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _plain(value: Any) -> dict:
    if value is None:
        return {}
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return dict(value)


class StripeGateway:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self._client = stripe.StripeClient(
            api_key,
            max_network_retries=NETWORK_RETRIES,
            http_client=stripe.HTTPXClient(timeout=TIMEOUT_SECONDS),
        )

    async def _call(self, what: str, method: Any, *args: Any, **kwargs: Any) -> dict:
        try:
            return _plain(await method(*args, **kwargs))
        except stripe.AuthenticationError as exc:
            logger.error("[payments] %s: Stripe rejected the API key (%s)", what, type(exc).__name__)
            raise GatewayError("stripe_auth") from exc
        except stripe.APIConnectionError as exc:
            logger.error("[payments] %s: could not reach Stripe", what)
            raise GatewayError("stripe_unreachable") from exc
        except stripe.RateLimitError as exc:
            logger.error("[payments] %s: Stripe rate limit", what)
            raise GatewayError("stripe_busy") from exc
        except stripe.StripeError as exc:
            logger.error(
                "[payments] %s failed: %s %s",
                what,
                type(exc).__name__,
                getattr(exc, "code", None) or "",
            )
            raise GatewayError("stripe_error") from exc

    # Checkout ---------------------------------------------------------------

    async def retrieve_session(self, session_id: str) -> dict:
        return await self._call(
            "checkout.sessions.retrieve",
            self._client.v1.checkout.sessions.retrieve_async,
            session_id,
        )

    async def create_session(self, params: dict, idempotency_key: str) -> dict:
        return await self._call(
            "checkout.sessions.create",
            self._client.v1.checkout.sessions.create_async,
            params,
            {"idempotency_key": idempotency_key},
        )

    # Connect ----------------------------------------------------------------

    async def retrieve_account(self, account_id: str) -> dict:
        return await self._call(
            "accounts.retrieve", self._client.v1.accounts.retrieve_async, account_id
        )

    async def create_account(self, params: dict, idempotency_key: str) -> dict:
        return await self._call(
            "accounts.create",
            self._client.v1.accounts.create_async,
            params,
            {"idempotency_key": idempotency_key},
        )

    async def create_account_link(self, params: dict) -> dict:
        return await self._call(
            "account_links.create", self._client.v1.account_links.create_async, params
        )

    async def create_login_link(self, account_id: str) -> dict:
        return await self._call(
            "accounts.login_links.create",
            self._client.v1.accounts.login_links.create_async,
            account_id,
        )


_gateway: Optional[StripeGateway] = None


def get_gateway() -> Optional[StripeGateway]:
    """The Stripe gateway, or None when STRIPE_SECRET_KEY is not set."""
    global _gateway
    key = settings.STRIPE_SECRET_KEY
    if not key:
        return None
    if _gateway is None or _gateway.api_key != key:
        _gateway = StripeGateway(key)
    return _gateway
