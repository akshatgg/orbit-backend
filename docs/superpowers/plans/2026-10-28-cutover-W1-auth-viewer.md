# W1: Auth, session and viewer endpoints (sub-plan)

Parent plan: `2026-10-28-supabase-cutover.md`, section W1 (requirements 1–9).

**Goal:** close the backend gaps the frontend needs for sign-in, the event page and the
viewer's own state, so the frontend can drop its Supabase calls for these screens.

**Verification:** no test files. Each task is checked by running the API on a scratch copy
of production data (`orbit_w1`, port 8101) and curling the endpoint, including the
unauthorised case. Commands and trimmed responses go in the PR body.

Setup used by every task:

```bash
WT=/Users/akshat/projects/orbits/orbit-backend-wt/w1
PY=/Users/akshat/projects/orbits/orbit-backend/.venv/bin
docker exec -i orbit-backend-postgres psql -U orbit -c "create database orbit_w1"
DATABASE_URL=postgresql://orbit:orbit@localhost:5433/orbit_w1 $PY/alembic upgrade head
docker exec -i orbit-backend-postgres psql -U orbit -d orbit_w1 -q < seeds/prod.sql
ENVIRONMENT=local DATABASE_URL=postgresql://orbit:orbit@localhost:5433/orbit_w1 JWT_SECRET=dev \
  $PY/uvicorn app.main:app --port 8101
# token for a real user id (app's own helper):
JWT_SECRET=dev $PY/python -c "from app.core.security import create_access_token as c; print(c('<id>','<email>'))"
```

No migration is needed: every field used below already exists in the baseline schema.

---

## Task 1: Cookie domain (req 1)

Files: `app/core/config.py`, `app/auth/router.py`, `.env.example`, `deploy/vm/settings.env`.

- `SESSION_COOKIE_DOMAIN: Optional[str] = None` in settings (Auth block).
- `_set_cookie` passes `domain=settings.SESSION_COOKIE_DOMAIN or None`.
- New `_clear_cookie(response)` used by `/auth/signout`: deletes the cookie with the domain;
  when a domain is configured it also deletes the host-only cookie, so a session set before
  the switch cannot linger.
- `.env.example`: commented `SESSION_COOKIE_DOMAIN=` (blank = host-only, right for localhost).
- `deploy/vm/settings.env`: `SESSION_COOKIE_DOMAIN=.akshatgg.in` (not secret).

Verify: run once with `SESSION_COOKIE_DOMAIN=.example.test`, `curl -i -X POST /api/auth/login`
→ `set-cookie: orbit_session=...; Domain=.example.test; HttpOnly; Path=/; SameSite=lax`;
`curl -i -X POST /api/auth/signout` → two expiring `set-cookie` lines.

## Task 2: `POST /auth/login` sets the cookie (req 2)

File: `app/auth/router.py`. Add `response: Response` to `login`, call `_set_cookie(response, token)`,
return the same `AuthResponse` JSON.

Verify: set a password for a seeded user through `POST /api/auth/password` (bearer),
then `curl -i -X POST /api/auth/login -d '{"email","password"}'` → 200, JSON body unchanged,
`set-cookie` present; then `curl --cookie orbit_session=<value> /api/auth/session` → 200.
Wrong password → 401, no cookie.

## Task 3: Redirect on failed confirm / Google callback (req 3)

File: `app/auth/router.py`.

- `/auth/confirm`: `token` no longer has `min_length` (a short token is just an invalid link);
  when `consume_token` returns None → `303 FRONTEND_URL/login?error=link`.
- `/auth/google/callback`: on `error`, missing `code`/`state`, a bad state, or any
  `HTTPException`/`httpx.HTTPError` from the exchange → `303 FRONTEND_URL/login?error=auth`.

Verify: `curl -i '/api/auth/confirm?token=bogus'` → 303 `location: http://localhost:3000/login?error=link`;
`curl -i '/api/auth/google/callback?error=access_denied'` and `?code=x&state=bad` → 303 `...?error=auth`.

## Task 4: `GET /events/counts` (req 4)

Files: `app/events/router.py`, `app/events/schemas.py`, `app/events/service.py`.

- Shared parser `svc.parse_ids(raw: str, limit=200) -> list[UUID]`: comma separated,
  blanks dropped, duplicates removed in order, 422 on a bad UUID or more than 200.
- Route declared before `/{event_id}`; public. Returns one row per requested id (zeros when
  the event has no registrations), from `fn.event_counts`.
- `EventCountsRow(BaseModel)`: `event_id: UUID, going: int, waitlisted: int, pending: int`.

Verify: two ids (one with a registration) → list of two rows; bad uuid → 422; 201 ids → 422;
missing `ids` → 422. It is public, so there is no unauthorised case.

## Task 5: `GET /me/event-state` (req 5)

Files: `app/profiles/router.py`, `app/profiles/service.py` (new), `app/profiles/schemas.py` (new).

- `require_user`; `event_ids` optional (omitted → every mark and registration of the user,
  which is what the frontend's `getMyEventState(userId)` does); max 200 via `svc.parse_ids`.
- Reads `user_events (event_id, kind)` and `registrations (event_id, status)` for the user.
- `{"saved": [], "dismissed": [], "external_registered": [], "attended": [], "registrations": {id: status}}`.

Verify: no token → 401; with token, mark an event saved (`PUT /events/{id}/marks/saved`),
call with and without `event_ids`; bad id → 422.

## Task 6: `GET /events/{id}` (req 6)

Files: `app/events/service.py`, `app/events/schemas.py`.

- `detail()` reads `event_private` when the viewer's registration is `going` **or** `can_manage`,
  and returns it only when `online_url` or `guest_info` is non-empty (the frontend rule).
- `ticket_types[]` items gain `remaining: int | null` from `fn.ticket_availability`
  (`EventDetail.ticket_types: list[TicketTypeAvailability]`).

Verify: insert an `event_private` row for an event with a ticket type; anonymous GET →
`event_private: null`, `ticket_types[].remaining` present; super admin token → `event_private`
filled, `can_manage: true`; plain user → null.

## Task 7: `/me/calendar` and `/me/events` (req 7)

Files: `app/profiles/service.py`, `app/profiles/router.py`, `app/profiles/schemas.py`.

Both now return `svc.card` items (the `/events` `EventCard` shape, with `counts`
going/waitlisted/pending) plus the viewer's flags:
`saved, attended, external_registered: bool`, `registration: str | null`.

- `/me/calendar?from=&to=` (defaults now → +90 days): ids = saved ∪ external_registered ∪
  active registrations (`going|pending|waitlisted|awaiting_payment`); events with status
  `published` or `cancelled`, not ended at `from` (`coalesce(ends_at, starts_at) >= from`),
  `starts_at < to`, soonest first, limit 500. `registration` only when active. Mirrors
  `listMyCalendar` in the frontend.
- `/me/calendar.ics`: from now, published only (the frontend export rule).
- `/me/events?when=upcoming|past`: marks `saved|attended|external_registered` ∪ active
  registrations; status not `draft`/`removed`; `upcoming` = not ended (soonest first),
  `past` = ended (latest first). Mirrors `getMyEventLists`.

Verify: 401 without token; seed a saved mark, an external_registered mark and a cancelled
event the user saved; `/me/calendar` shows the cancelled one and the external one;
`/me/events` shows `counts` with three keys; `/me/calendar.ics` returns VEVENTs.

## Task 8: `GET /invites/{code}` (req 8)

File: `app/organisers/service.py` (`peek_invite`).

- Unknown code → 404. Otherwise 200:
  `{"organiser": {"id","name","slug","logo_url","status"}, "role", "expires_at", "revoked",
  "max_uses", "uses", "uses_left", "already_member", "valid", "reason"}`.
- `reason` in the frontend's order: `revoked`, `expired`, `used_up`, `org_inactive`
  (organiser not approved; the frontend shows it today), else null; `valid = reason is None`.
- `uses_left = max(0, max_uses - uses)` (the column is not nullable, so never null in practice).

Verify: 401 without token; unknown → 404; insert a valid, an expired, a revoked and a used-up
invite → 200 with the right `reason`.

## Task 9: Typed `PATCH /me` and `POST /me/onboarding` (req 9)

Files: `app/profiles/schemas.py`, `app/profiles/router.py`, `app/profiles/service.py`.

`ProfileUpdate(BaseModel, extra="forbid")`, every field optional, the editable columns of
`profiles` (`app/users/models.py`): `full_name, avatar_url, university, department, course,
year_of_study, graduation_year, user_type, goals, career_interests, personal_interests,
preferred_categories, preferred_formats, location_preference, availability, company,
industry, job_title, host_role`. Rules mirror the frontend's zod schema: trimmed text, `""` → null,
length caps; `goals`/`user_type`/`preferred_formats` as Literals (the DB check constraints);
lists de-duplicated; `availability` normalised to known days and parts (empty → null);
`graduation_year` 1950–2100.

`preferred_categories` is filtered to slugs in `categories` (`keepKnownCategories`).
PATCH writes only the fields sent (`exclude_unset`); an empty PATCH stays 422. Onboarding
writes the fields sent and sets `onboarded_at`.

The parent plan names `bio`, `interests`, `city`, `country` and `remind_before_hours`; none
of these are `profiles` columns. `remind_before_hours` lives in `notification_preferences` and
is already typed (`Literal[2, 24]`) on `PUT /me/notification-preferences`. They are left out
(an unknown key is a 422 under `extra="forbid"`).

Verify: 401 without token; unknown key → 422; bad goal → 422; PATCH with
`preferred_categories: ["<real slug>", "ghost"]` → stored `["<real slug>"]`; onboarding sets
`onboarded_at`.

## Finish

- `ruff check app` clean.
- Push `feat/cutover-w1-auth-viewer`, open the PR to `master` with the frontend contract and
  the evidence. Stop uvicorn, drop `orbit_w1`.
