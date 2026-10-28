# W8: Backend parity fixes found by the frontend cutover run

Sub-plan of `2026-10-28-supabase-cutover.md`. Built on every unmerged backend PR (#6, #8–#14,
merged in that order, W2/W3 conflict in `app/studio/service.py` resolved keeping both the
`event_full` mapping and the notify calls); rebased onto `master` once they land.

**Goal:** close the gaps the frontend PR hit when it ran against all backend PRs together, so each
endpoint answers the way the Supabase-era frontend behaved.

**Source of truth (frontend `origin/master`, read-only):** `src/lib/tickets/queries.ts`
(`getEvent`, `listMyTickets`, `getMyTicket`), `supabase/migrations/20261016000000_init.sql`
(`events_select`, `organisers_select` RLS), `src/lib/profile/queries.ts` (`getFollowedOrganisers`),
`src/lib/discovery/queries.ts` (`getDiscoveryCounts`, `listUpcomingPlaces`), `src/lib/places.ts`
(`groupPlaces`), `src/lib/seo/public-data.ts` (`getEventForOg`), `src/app/admin/users/page.tsx` +
`src/lib/admin/users.ts`, `src/app/admin/organisers/page.tsx`, `src/app/admin/events/actions.ts` +
`src/lib/admin/event-form.ts`, `src/app/admin/sources/actions.ts`, `src/lib/sources/queries.ts`,
`src/lib/organiser/{org-actions,event-form}.ts`, `src/components/organiser/TeamPanel.tsx`,
`src/lib/notifications/channels.ts`.

## Files

| File | Change |
|---|---|
| `app/admin/router.py` | category PATCH maps `group` → `group_name`; users `goals`, `q` over university/course, type filter as the frontend; organisers applicant/reviewer/Stripe fields |
| `app/admin/{schemas,service}.py` | admin events take `source_url`; created events are `source="manual"` |
| `app/events/{router,service,schemas}.py` | event page visibility = the old `events_select` RLS; `latitude`, `longitude`, `cancel_reason` |
| `app/organisers/service.py` | SQL refusals → 400/403/404/409 `{"detail": sentence}`; existing member → 409 `already_on_team` |
| `app/profiles/router.py` | `/me/tickets` nesting + not-ended split; `/me/following` `kind`, `bio`, newest follow first, RLS-visible organisers |
| `app/discovery/{service,router}.py` | counts over every interest, series once; places grouped like `groupPlaces` (hybrid included) |
| `app/discovery/places.py` | the one places module (W7's `app/sources/places.py` merged in, file removed) |
| `app/sources/interests.py` | interest values come from `app/discovery/taxonomy.py` |
| `app/public/service.py` | OG: `format`, `status`; cancelled events served |
| `app/sources/router.py` | run returns `duplicates`; review summaries name organiser and category; `manual` label |
| `app/studio/{schemas,service}.py` | `price_min`/`price_max` for external/none events |
| `app/core/routing.py` (new) + routers | `NumbersRoute`: Decimal columns serialise as JSON numbers |
| `app/payments/service.py` | connect status `fee_percent` |
| `app/notifications/channels.py` | no provider = email not available (the local log channel only prints) |
| `app/core/deps.py` | `require_cron` uses `hmac.compare_digest` |

## Tasks (one commit each)

1. **Category PATCH 500.** `values["group_name"] = values.pop("group")` before the ORM update.
   Verify: `PATCH /admin/categories/{id} {"group":"social"}` 500 before, 200 after; 401/403.
2. **Event page visibility.** Visible when `status in (published, cancelled, expired)` and the organiser
   is null or approved, or the caller can manage it (`can_manage_event`). Drafts/removed stay 404.
   `EventDetail` adds `latitude`, `longitude`, `cancel_reason`. Verify on a cancelled, an expired, a
   draft and a removed event (anon, user, admin).
3. **SQL refusals.** `remove_member`, `add_member`, `accept_invite` catch `DBAPIError` with SQLSTATE
   P0001 and answer with the raised sentence: 409 for the last owner, 404 for "not on this team" and
   "invite not valid", 403 for "Only owners…", 401 for "Sign in…", 400 otherwise. Verify each.
4. **Adding an existing member.** The old UI only ever "added" (TeamPanel "Add a team member",
   message "… added to the team", no role control); the role change was a side effect of the SQL
   upsert, which could demote the last owner. → 409 `already_on_team`, role untouched.
5. **My tickets.** Rows keep their flat fields and add `ticket_type_id`, `ticket{id,name,price,currency}`,
   `event{id,title,summary,cover_url,starts_at,ends_at,format,venue_name,address,city,status,
   cancel_reason,registration_mode,organiser{id,slug,name,logo_url,is_verified}}`. `tab=upcoming` =
   not ended (`coalesce(ends_at, starts_at) >= now()`), soonest first; `past` newest first. Only events
   the old RLS showed. Verify with a registration on a started-not-ended event.
6. **Following.** Add `kind`, `bio`; newest follow first; organisers the viewer could see (approved,
   created by them, member, admin).
7. **Discovery.** Counts pass every interest (`ALL_INTERESTS` + synonyms) and take `total` from the
   function's series-collapsed `all` row; places group `format <> 'online'` rows with `place_of`,
   a series once, unknown countries last as "Other places". Compare with SQL.
8. **OG.** `format`, `status`; `published` or `cancelled` with an approved/no organiser.
9. **Admin users.** `goals`; `q` over email, name, university, course; `type=student|other` include
   legacy rows, `type=host` = `goals @> {host}`.
10. **Admin extras.** Organisers: `stripe_charges_enabled`, `created_by`, `created_by_name`,
    `created_by_email`, `reviewed_by_name`, `q` over name/slug/contact email/university. Events:
    `source_url`; create stores `source="manual"`. Imported details: confirm `image_url` takes W7's
    import path (disabled path without keys). Run: `duplicates`. Review summaries:
    `organiser{id,name}`, `category{id,name}`.
11. **Studio numbers.** `EventCreate`/`EventUpdate` accept `price_min`/`price_max` (ignored for
    `tickets`, where the `sync_event_price` trigger derives them). `NumbersRoute` on the routers that
    return raw dicts (Decimal → number). Connect status `fee_percent`.
12. **Email counts.** `email_available()` is true only for SMTP/Resend, as in the frontend's
    `isEmailConfigured`; the local log channel still prints magic links but reports "not sent".
13. **De-duplicate.** One places module (`app/discovery/places.py`), one interest list
    (`app/discovery/taxonomy.py`). Check: W7 `with_interests`/`normalise_country` over every prod event
    and `GET /events` searches give identical output before and after.
14. **Cron secret.** `hmac.compare_digest`.

Each task: `ruff check app`, then curl on `orbit_w8` (port 8108) with the unauthorised case.
Finish: push, PR (depends on #6, #8–#14), stop uvicorn, drop `orbit_w8`.
