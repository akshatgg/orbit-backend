# W4: Payments parity — sub-plan

Parent: `2026-10-28-supabase-cutover.md`, section W4 (requirements 1–6). Global
Constraints of the parent plan apply: one PR to `master`, no test files, no
secrets or infrastructure details, verify for real on `orbit_w4` / port 8104.

**Source of truth (frontend, read-only):** `src/lib/payments/{index,fulfil,logic,connect,stripe}.ts`,
`src/app/api/stripe/webhook/route.ts`, `src/app/api/stripe/connect/refresh/route.ts`,
`src/app/checkout/success/page.tsx`, `src/app/(main)/organiser/settings/payments/actions.ts`.

## Files

| File | Change |
| --- | --- |
| `app/core/config.py` | `STRIPE_CONNECT_WEBHOOK_SECRET`, `PLATFORM_FEE_PERCENT`; properties `stripe_webhook_secrets`, `platform_fee_percent` |
| `app/payments/logic.py` (new) | Pure helpers, a line-for-line port of `logic.ts` |
| `app/payments/gateway.py` (new) | The only code that talks to Stripe. `get_gateway()` returns `None` when unconfigured; every method returns plain dicts and raises `GatewayError(code)` |
| `app/payments/service.py` | Checkout, fulfilment, success page, Connect, webhook |
| `app/payments/router.py` | Connect refresh becomes a 303; webhook returns 500 on a released claim |
| `.env.example`, `deploy/vm/bin/render-env`, `deploy/vm/settings.env` | New settings |

## Task 1: settings

- `STRIPE_CONNECT_WEBHOOK_SECRET: Optional[str] = None` (secret, optional).
- `PLATFORM_FEE_PERCENT: str = "0"`, parsed like `parseFeePercent` (`"5"`, `"5%"`, junk → 0, capped at 100) so a typo can never stop the app booting.
- `stripe_webhook_secrets`: both secrets, each comma-split, trimmed, blanks dropped (`webhookSecrets()`).
- `render-env` passes `STRIPE_CONNECT_WEBHOOK_SECRET` and `PLATFORM_FEE_PERCENT` through when the secret exists; `.env.example` documents both.

## Task 2: pure logic (`logic.py`)

Port of `logic.ts`: `HOLD_MINUTES=30`, `CHECKOUT_MINUTES=31`, `SUPPORTED_CURRENCY="gbp"`,
`to_pence`, `parse_fee_percent`, `application_fee_pence`, `line_item_name` (250 cap with `…`),
`checkout_expires_at`, `is_reusable_session` (open, has url, >2 min left),
`checkout_blocker` (returns a stable code instead of English copy), `connect_state`,
`is_organiser_payable`, `checkout_outcome`, `should_fulfil`.

## Task 3: Stripe boundary (`gateway.py`)

`StripeGateway` over `stripe.StripeClient(key, max_network_retries=2, http_client=HTTPXClient(timeout=20))`,
async methods: `retrieve_session`, `create_session(params, idempotency_key)`, `retrieve_account`,
`create_account(params, idempotency_key)`, `create_account_link`, `create_login_link`.
Stripe errors map to `GatewayError` codes `stripe_auth` / `stripe_unreachable` / `stripe_busy` / `stripe_error`
(the four branches of `describeStripeError`). The service resolves the gateway through
`gateway.get_gateway()` at call time, so a verification script can swap it for a fake.

## Task 4: checkout (requirement 1)

`start_checkout(db, registration_id, user)` in `createTicketCheckout` order:
1. no gateway → 503 `payments_not_configured`;
2. registration (with event, ticket type) owned by caller, else 404 `registration_not_found`;
3. `checkout_blocker` → 409 code (`already_paid`, `already_going`, `awaiting_approval`, `waitlisted`, `not_awaiting_payment`, `free_ticket`, `unsupported_currency`);
4. event published (409 `event_not_on_sale`), not ended (409 `event_ended`), has an organiser (409 `no_organiser`);
5. reuse the stored session when `is_reusable_session`; if it is already paid, fulfil and return the success URL;
6. organiser `approved` (409 `organiser_not_approved`) and payable (409 `stripe_not_ready`);
7. `payment_hold_ok` (409 `hold_expired`);
8. create the session: GBP line item, `customer_email`, `client_reference_id`, metadata, `payment_intent_data.transfer_data.destination`, `application_fee_amount` when > 0, `expires_at`, `locale=en-GB`, idempotency key `orbit-checkout-<id>-<session|new>-<updated_at>`;
9. save the session id (refreshes the hold) → `{"checkout_url": url}`.

## Task 5: fulfilment and webhook (requirement 2)

- `fulfil_checkout_session(db, session)` → `"marked" | "already" | "skipped"`, a port of `fulfil.ts`, with `select … for update` so the webhook and the success page cannot both mark the same row. After `"marked"` it calls `_registration_paid()`, the no-op hook W3 replaces with `notify_registration_changed`.
- `handle_webhook`: 503 without a gateway or secrets; 400 `missing_signature` / `invalid_signature` (each secret tried in turn, `stripe.WebhookSignature.verify_header`, default 300 s tolerance); claim via `insert … on conflict do nothing returning id` (duplicate → `{"received": true, "duplicate": true}`); handle `checkout.session.completed` / `checkout.session.async_payment_succeeded` (fulfil only when `payment_status == "paid"`) and `account.updated` (sync `stripe_charges_enabled`); the handler runs in a SAVEPOINT and on failure the savepoint is rolled back, the claim deleted and 500 returned so Stripe's retry is processed.

## Task 6: success page, Connect (requirements 3–5)

- `GET /checkout/success`: 503 when unconfigured; malformed or unloadable session → `outcome: "unknown"`; not the caller's registration → 403; `paid` → fulfil; response shape in the PR.
- `GET /payments/connect/status`: live `accounts.retrieve` + sync (`refreshAccount`), falls back to the stored flag; `state` from `connect_state`.
- `POST /payments/connect/onboarding`: approved organisers only (403 `organiser_not_approved`); `ensureConnectedAccount` port (GB Express, idempotency key, compare-and-set store); link `refresh_url`/`return_url` = `FRONTEND_URL/organiser/settings/payments?org=<id>&stripe=refresh|return`.
- `GET /payments/connect/refresh`: 303 to a fresh onboarding link; 303 back to the payments page when unconfigured or no account, with `&error=link` when Stripe fails. Never creates an account.

## Task 7: verification (no Stripe traffic)

1. `createdb orbit_w4`, `alembic upgrade head`, load `seeds/prod.sql`; run uvicorn on 8104 (`ENVIRONMENT=local JWT_SECRET=dev`, no Stripe keys).
2. Disabled paths with curl: checkout 503, success 503, status `state: unconfigured`, onboarding 503, refresh 303 back, webhook 503; unauthenticated 401 and non-owner 403.
3. Restart through a scratch launcher under `/tmp` that installs a fake gateway (records calls, never opens a socket) with dummy `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET`:
   - guards: unapproved organiser, charges not enabled, hold lapsed with no room;
   - happy path: request parameters show destination, fee, expiry and idempotency key; session saved; reuse returns the same URL;
   - webhook: missing and bad signature → 400; a correctly signed `checkout.session.completed` (HMAC-SHA256 over `t.payload`) marks the registration paid once; replay → duplicate; an injected failure leaves no `stripe_events` row and Stripe's retry then succeeds; `account.updated` syncs the flag;
   - success page shapes for paid / processing / expired / unknown.
4. `ruff check app`, then stop uvicorn and drop `orbit_w4`.
