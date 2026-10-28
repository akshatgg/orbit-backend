# W3: Notifications and web push

Sub-plan of `2026-10-28-supabase-cutover.md` (section W3). Branch
`feat/cutover-w3-notifications`, built on W5 (#8), W1 (#9), W4 (#10) and W2 (#11).

**Goal:** the backend sends every notification the frontend sends today, with the
same words, to the same people, through the same channels (web push and email),
deduped through `notification_log`, so the frontend can drop
`src/lib/notifications` and `src/app/api/push/test`.

**Source of truth (frontend, read-only):** `src/lib/notifications/{index,deliver,plan,channels,messages,templates,email-config,reminders}.ts`,
`src/app/api/push/*`, `src/app/api/cron/reminders/route.ts`, and the call sites in
`src/app/(main)/organiser/events/[id]/actions.ts`, `src/lib/organiser/event-actions.ts`,
`src/app/admin/organisers/actions.ts`, `src/app/admin/events/actions.ts`,
`src/lib/payments/fulfil.ts`.

## Design

- **When sending happens.** The frontend sends after the response (`after()`).
  Here the three `notify_*` functions queue a job on the caller's session and
  the job runs after that session's outermost transaction commits, as an asyncio
  task with its own session. A rolled-back transaction (or savepoint) drops its
  jobs, so nobody hears about a change that never happened. Queuing and running
  are both wrapped: a notification problem is logged, never raised into the
  caller.
- **Delivery pipeline** (`deliver.ts`): claim one `notification_log` row per
  (user, event, kind, channel) with `on conflict do nothing`; send push (8 people
  at a time) and email; delete the claims of failed sends so a later run retries.
- **Channels** (`channels.ts`, `email-config.ts`):
  - Push via `pywebpush` (VAPID keys from settings, TTL 12h, urgency normal).
    Every subscription of the user; 404/410 deletes the subscription.
  - Email via SMTP when `SMTP_HOST/SMTP_USER/SMTP_PASS` are set, else Resend
    (batch API, 100 per call), else no email channel. Locally (`ENVIRONMENT=local`)
    with no provider the rendered email is logged and counted as sent, so the
    ledger and the copy can be checked without sending anything.
- **Copy** (`messages.ts`, `templates.ts`): ported verbatim, UK-time formatting
  (`Thu 5 Nov · 18:00`, `Thursday, 5 November 2026`, `18:00 – 20:00`).
- **Reminders** (`plan.ts`, `reminders.ts`): each user's `remind_before_hours`
  picks their window, `max(h + interval/2, interval)` with the daily cron
  (24 → 36h, 2 → 24h); deadline reminders for saved, unregistered events whose
  deadline is within 48h; push and email.

## Tasks

1. `requirements.txt`: `pywebpush>=2.0`. Settings: `SMTP_HOST`, `SMTP_PORT`
   (465), `SMTP_USER`, `SMTP_PASS`; `EMAIL_FROM` becomes optional with the
   frontend's fallbacks. `.env.example`, `deploy/vm/bin/render-env` and
   `deploy/vm/import-vercel-secrets.sh` gain the SMTP names.
2. `app/notifications/messages.py`: `Message`, the formatters, every message
   builder and `render_email` (HTML + text).
3. `app/notifications/channels.py`: `send_push`, `send_emails`, `email_channel`,
   `channels_available`.
4. `app/notifications/service.py`: `load_recipients`, `channels_for`, `deliver`,
   the after-commit queue, and
   - `notify_registration_changed(db, registration_ids, reason)` (classifies the
     row like `classifyRegistration`; reason `waitlist` marks a promotion),
   - `notify_event_changed(db, event_id, change)` (replaces the unwired
     `notify_event_change`; recipients = active registrations + saved /
     external_registered; kind `event_cancelled` or `event_updated:<fingerprint>`),
   - `notify_organiser_decision(db, organiser_id, decision)` (owners; approved /
     rejected only; no ledger row because `notification_log.event_id` is required,
     so the caller only notifies when the status actually changes).
   `send_email` (magic links) goes through the same email channel.
5. Call sites:
   - `studio.service.decide` (and the waitlist promotions a decline causes),
     `approve_all`, `cancel_event`, `update_event` (published event whose time,
     end, venue, address or format changed).
   - `payments.service._registration_paid` (webhook and `/checkout/success`).
   - `admin` event cancel and edit (logistics change on a published event), and
     organiser decision (approved / rejected, status changed).
6. `app/cron/service.py`: reminders ported from `plan.ts` + `reminders.ts`;
   response `{events, reminders, push_sent, emails_sent, skipped, failed}`.
7. `POST /push/test` sends a test push like the frontend route (503 when push is
   not configured, 400 with no devices, 502 when nothing was delivered).
   `POST /push/subscriptions` accepts `old_endpoint` (rotation, as the service
   worker does today).

## Verification (database `orbit_w3`, port 8103, no real email or push)

- `uvx ruff@0.8.6 check app`.
- Throwaway VAPID pair; a fake push service under `/tmp` (HTTPS, self-signed
  cert trusted through `SSL_CERT_FILE`) that records and decrypts each request
  and answers 201 or 410 per endpoint.
- Synthetic users (`*@example.test`) with push subscriptions and preferences:
  decision (approve / decline + waitlist promotion), approve-all, paid through a
  locally signed webhook event, organiser and admin cancel, logistics update,
  admin organiser approve / reject, `/push/test`; each shows the email log lines
  and push requests for exactly the right people, a second trigger deduped, and
  a 410 deleting the subscription.
- Reminders with users on 2h and 24h and events at +20h / +30h.
- A forced failure inside a notification job leaves the API response 200.
- Unauthorised calls: 401 on `/push/test`, `/push/subscriptions`; 401/403 on
  the cron and studio/admin triggers.
- Stop uvicorn, drop `orbit_w3`.
