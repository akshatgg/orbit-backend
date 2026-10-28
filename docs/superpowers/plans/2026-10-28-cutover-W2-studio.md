# Cutover W2: Organiser studio parity and safety

Sub-plan of the Supabase cutover program, workstream W2. One branch
(`feat/cutover-w2-studio`), one PR to `master`. No schema change, so no Alembic
revision. No test files: every task is verified by running the API on a scratch
database loaded with `seeds/prod.sql` and calling it with `curl`.

The behaviour mirrors the frontend's Supabase implementation, which the studio
uses today: `src/lib/organiser/event-actions.ts` (`saveEvent`, `createMoreDates`,
`syncChildren`, `cancelEvent`), `src/lib/organiser/event-form.ts` (`formForDate`),
`src/lib/organiser/repeat.ts` (`weeklyLabel`) and `src/lib/organiser/queries.ts`.

## Files

| File | Change |
| --- | --- |
| `app/studio/schemas.py` | `id` on `TicketTypeIn`/`QuestionIn` (unique per list); `DateSlotIn`, `DatesIn` |
| `app/studio/service.py` | studio event list, guards, diff-based child sync, more dates, organiser in detail, cancel/publish status rules |
| `app/studio/router.py` | `GET /organiser/events`, `POST /organiser/events/{id}/dates` |
| `app/organisers/service.py` | `public_events_page(..., when)` |
| `app/organisers/router.py` | `when=upcoming\|past\|all` query parameter |

## Error contract (all `{"detail": "<code>"}`)

| Status | Code | When |
| --- | --- | --- |
| 403 | `organiser_not_approved` | create, PATCH, publish or add dates while the organiser's `status != "approved"` |
| 409 | `stripe_not_ready` | the event is (or is being) published, uses tickets, has a ticket with `price > 0`, and the organiser has no `stripe_charges_enabled` |
| 409 | `has_registrations` | PATCH moves `registration_mode` off `tickets` while registrations are `going/pending/waitlisted/awaiting_payment` |
| 409 | `ticket_type_has_guests` | PATCH drops a ticket type that has an active registration |
| 409 | `event_not_editable` | PATCH or add dates on a `cancelled`/`removed` event (`saveEvent` refuses these) |
| 409 | `event_not_publishable` | publish an event that is not a draft (published is a no-op) |
| 409 | `event_not_published` | cancel an event that is not `published` |
| 422 | `date_in_past`, `duplicate_date` | add dates with a past start, or one the series already has |

Active registration statuses are the frontend's `ACTIVE_REGISTRATION`:
`going, pending, waitlisted, awaiting_payment`.

Paid tickets are only refused when the event goes live, exactly as
`validateEventForm` does (`publishing && price > 0 && !chargesEnabled`): drafts may
carry paid tickets while Stripe onboarding finishes.

## Task 1: Studio event list

`GET /organiser/events?organiser_id=<uuid>&page=&page_size=` (member of the
organiser, admins allowed; `org_role`, the same check `require_org_member` makes,
used so a malformed id is a 422 instead of a database error).

- Every event of the organiser in any status, `starts_at desc`.
- Each item: `EVENT_COLUMNS` (includes `series_key`, `series_label`) + `counts`.
- `page_size` default 500, max 500 (the studio groups client-side; the frontend
  loaded up to 500).
- Response `{"items": [...], "total": int, "page": int, "page_size": int}`.

Verify: 401 without a token; 403 for a non-member; 200 for the owner, items
newest first with counts.

## Task 2: Public organiser events `when`

`GET /organisers/{id}/events?when=upcoming|past|all` (public, default `upcoming`).

- `upcoming`: `published`, not ended, `starts_at asc` (unchanged).
- `past`: `published/cancelled/expired` that ended (or are `expired`), `starts_at desc`.
- `all`: `published/cancelled/expired`, `starts_at asc`.
- Drafts and removed events never appear; duplicates are hidden as before.

Verify: each value; a draft fixture never appears; `when=bogus` is 422.

## Task 3: Diff-based ticket types and questions

`TicketTypeIn.id` / `QuestionIn.id: UUID | None`. `EventUpdate` rejects a list
that repeats an id (422).

`_sync_children(db, table, event_id, rows, currency, protect)` mirrors
`syncChildren`:

1. read the event's current ids;
2. `keep` = submitted ids that belong to this event; anything else is inserted;
3. `remove` = current ids not kept; for ticket types, any active registration on
   a removed id → 409 `ticket_type_has_guests`;
4. delete `remove`, update kept rows in place, insert new rows, returning the ids
   in submitted order.

Registrations keep their `ticket_type_id` because kept ticket rows are updated,
never deleted (the FK is `on delete set null`, which is what the old
delete-and-reinsert silently did to every guest).

PATCH and POST return the event row plus `ticket_type_ids` and `question_ids`
(current ids in display order), matching `saveEvent`'s `ticketIds`/`questionIds`.

Verify: create a published event with two tickets and a question, register a
guest on ticket A, PATCH renaming A (with its id) and dropping B → same id for A,
guest's `ticket_type_id` unchanged; PATCH dropping A → 409; question ids survive.

## Task 4: Guards

- `_require_approved(db, organiser_id)` on create, PATCH, publish, dates.
- `_require_charges(...)` when the final state is published and uses tickets.
- PATCH: `event_not_editable`, `has_registrations`, `stripe_not_ready`,
  `ticket_type_has_guests`, all checked before any write (the request
  transaction rolls back on any error anyway).
- Publish: draft only (`published` returns the event unchanged), not started,
  approved, Stripe ready.
- Cancel: `update ... where status = 'published'`; 404 unknown, 409 otherwise.

Verify each with curl on fixtures: the pending organiser `headscale` (403), an
approved organiser without Stripe (409 on publish with a paid ticket), mode
change with a guest (409), cancel a draft (409).

## Task 5: More dates

`POST /organiser/events/{id}/dates` (`require_event_manager`), body
`{"dates": [{"starts_at": iso, "ends_at": iso | null}]}`, 1–25 dates (the
editor's `MAX_EXTRA_DATES`).

Mirrors `createMoreDates` + `formForDate`:

- parent must be `draft` or `published` (else 409 `event_not_editable`) and its
  organiser approved;
- each date: future start, end after start, not a date the series already has;
- `series_key` = the parent's, else `orbit:<parent id>` (the frontend's format);
- each sibling copies the parent's editor columns, `event_private`, ticket types
  and questions (new ids); `registration_deadline` and each ticket's
  `sales_end_at` move by the same number of local calendar days as the start
  (wall-clock in the event's time zone, so a weekly event keeps its local time
  across DST);
- status `published` when the parent is published (Stripe check first), else
  `draft`;
- `series_label` = `weeklyLabel` over every date in the series ("Every week on
  Wednesday until 9 December 2026" when consecutive dates are exactly 7 local
  days apart, else null), written to every event in the series.

Response 201 `{"series_key", "series_label", "count", "events": [{"id",
"starts_at", "ends_at", "status"}]}`.

Verify: dates on a published parent → published siblings with copied tickets,
questions, private details and shifted deadlines; label weekly; non-weekly →
null label; past date 422; 401/403.

## Task 6: Organiser in the editor payload

`GET /organiser/events/{id}` adds `"organiser": {"id", "name", "slug"}`.

## Finish

`ruff check app`; full curl run on `orbit_w2` (port 8102); PR with shapes,
error codes and evidence; stop uvicorn, drop `orbit_w2`.
