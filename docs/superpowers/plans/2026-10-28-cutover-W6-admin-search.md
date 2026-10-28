# W6: Admin extras and discovery/search parity

Sub-plan of `2026-10-28-supabase-cutover.md` (section W6). Built on the unmerged W5 (#8),
W1 (#9), W4 (#10) and W2 (#11) branches; rebased onto `master` once they land.

**Goal:** the admin console and Explore/For You pages get every read and write they do through
Supabase today from the backend, with the same results.

**Source of truth (frontend, read-only):** `src/lib/discovery/queries.ts`, `src/lib/ranking/index.ts`,
`src/lib/places.ts`, `src/lib/taxonomy.ts`, `src/lib/discovery/dates.ts`, `src/lib/events/series.ts`,
`src/lib/profile/user-type.ts`, `src/app/admin/events/page.tsx`, `src/lib/admin/{queries,series,labels}.ts`,
`src/app/admin/import/*`, `src/app/admin/sources/delete-source.ts`, `src/app/admin/events/actions.ts`.

## Files

| File | Change |
|---|---|
| `app/discovery/taxonomy.py` (new) | `CAREER_INTERESTS`, `PERSONAL_INTERESTS`, `ALL_INTERESTS`, `TAG_SYNONYMS`, `interest_tags` (port of `taxonomy.ts`) |
| `app/discovery/places.py` (new) | `KNOWN_CITIES`, `city_variants`, `known_city_names`, `clean_location` (port of the parts of `places.ts` search uses) |
| `app/discovery/dates.py` (new) | `date_preset_range` (UK-local `today`/`week`/`weekend`/`month`) |
| `app/discovery/rank.py` (new) | port of `ranking/index.ts` (signals, weights, reasons, exclusions, `rank_events`), `sort_results`, `load_viewer` |
| `app/events/service.py` | interest list now the frontend taxonomy; `next_dates_only` (SQL series collapse), `next_of_each_series`, `search_conditions`, `organiser_match`, `discovery_candidates`, `ranked_cards` |
| `app/events/schemas.py` | `EventSort` += `date`, `relevance`, `deadline`; `DatePreset`; `EventCard.score/reason`; `EventPage.truncated` |
| `app/events/router.py` | `GET /events` search parity; series collapse on `/events`, `/events/upcoming`, `/events/happening-soon` |
| `app/feed/{router,service}.py` | ranking port, series collapse, `days`, week grouped like the frontend |
| `app/admin/router.py` | `GET /admin/events/sources`, `series_dates`, `GET /admin/organisers/{id}/members`, import options with every organiser, bulk remove/delete semantics, frontend source labels, `when=all` newest first |
| `app/admin/schemas.py` | bulk remove reason optional |
| `app/sources/router.py` | `DELETE /admin/sources/{id}?events=keep|remove|delete` |

W3 edits `admin_organiser_decision` and `admin_cancel_event`: those functions are left untouched.

## Tasks

### 1. Taxonomy, places, dates, ranking (pure modules)
- Port literally; JS rounding (`Math.round`) as `floor(x + 0.5)`; UK weekday/hour via `zoneinfo`.
- `interest_tags(v)` = `[v, *synonyms mapping to v]`; `app/events/service.py` keeps the names
  `INTERESTS`, `INTEREST_LABELS`, `interest_tags` (re-exported) so `/interests` and
  `/discovery/counts` follow the frontend taxonomy.
- Commit: `feat(discovery): port the frontend taxonomy, places, date presets and ranking`.

### 2. `GET /events` search parity + series collapse
- New params: `date=today|week|weekend|month`, `format` repeatable, `sort=relevance|deadline|date`
  (plus existing `soonest|popular|newest`). `city` cleaned like `cleanLocation`, matched against
  every spelling (`ilike`), with `country` = that country or unknown. `country` alone also matches
  country-less events in its known cities. `q` matches the text search or an organiser whose name
  contains it (first 20 organisers, as the frontend).
- `soonest|date|popular|newest`: SQL, one row per series (its soonest matching date), paged, exact `total`.
- `relevance|deadline`: the frontend's candidate set (80 soonest text matches + 80 soonest organiser
  matches), series collapsed, ranked for the viewer (anonymous when signed out), sorted, paged;
  `truncated` when a candidate query hit 80.
- `/events/upcoming`: one row per series, soonest first (id tiebreak). `/events/happening-soon`:
  not-ended events starting in 14 days, 40 candidates, collapsed, date order, `limit` (default 12).
- Commit: `feat(events): search parity with the frontend and one card per series`.

### 3. Feeds
- `GET /feed/for-you?days=60` and `GET /feed/week?days=7`: 200 candidates, collapsed, ranked
  (dismissed and booked events dropped), each card with `score` and `reason`.
- Week: 3 top picks, then sections `career`, `social`, `other` by category group.
- Commit: `feat(feed): frontend ranking, series collapse and days on the feeds`.

### 4. Admin
- `GET /admin/events/sources` → `[{key, label}]` (orbit, manual, csv + every distinct source), sorted by label.
- `GET /admin/events` items: `series_dates: [{id, starts_at, status}]` for a series with 2+ dates
  in the listing (else null); `when=all` and `past` newest first, as the frontend.
- `GET /admin/organisers/{id}/members` → `[{user_id, role, created_at, full_name, email}]`; 404 unknown.
- `GET /admin/import/options` organisers carry `status`, every status.
- Bulk remove: `is_featured=false`, reason optional, one `event.remove` audit row per event.
  Bulk delete → `{deleted, registrations, images}` with one `event.delete` row per event
  (`permanent: true`); single delete adds `registrations` and `images`. 404 when no id matched.
- `DELETE /admin/sources/{id}?events=keep|remove|delete` (`remove_events=true` still means remove);
  `delete` hard-deletes the source's upcoming events, then `release_images` on their covers.
- Commit: `feat(admin): event sources, series dates, members, import options, bulk semantics, source delete`.

## Verification (database `orbit_w6`, port 8106)
- `uvx ruff@0.8.6 check app`.
- Search parity: a scratch script under `/tmp` runs the frontend's TypeScript (`rankEvents`,
  `sortResults`, `nextOfEachSeries`, `cityVariants`, `knownCityNames`, `datePresetRange`,
  `interestTags`) over rows read from the same database with the frontend's filters, and compares
  ids and order with the API, for ~10 queries signed in and out.
- Series collapse on `/events`, `/events/upcoming`, `/events/happening-soon`, `/feed/for-you`,
  `/feed/week`, and `days` on the feeds.
- Every admin endpoint with curl, including 401 (no token) and 403 (non-admin).
- PR body: changes, shapes, commands and results. Then stop uvicorn and drop `orbit_w6`.
