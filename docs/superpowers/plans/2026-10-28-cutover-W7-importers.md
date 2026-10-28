# W7: Event importers ported to Python — sub-plan

Part of the Supabase → backend cutover (program plan `2026-10-28-supabase-cutover.md`, section W7).
Requirements: the importer porting spec (§2–§14). Source of truth for behaviour: the TypeScript
engine on the frontend's `origin/master` (`src/lib/sources/`, `src/lib/places.ts`,
`src/lib/taxonomy.ts`, `src/lib/cloudinary.ts`), which is what production runs today.

**Goal:** the backend's `refresh-sources` produces the same rows the TypeScript engine does, so
the first Python run against production data updates ~0 unchanged events.

**Constraints:** stay in `app/connectors` and `app/sources` (plus the one-line cron router
hand-off to a background task). No new runtime dependency (stdlib, `httpx`, `idna` which httpx
already installs, `icalendar`, `python-dateutil`). No test files: golden comparisons run from
scratch scripts under `/tmp` against the TS code (`npx tsx`), never committed.

## Module map

| TypeScript | Python |
|---|---|
| JS string/number/date semantics (implicit) | `app/connectors/js.py` (UTF-16 lengths, JS `\s`/`trim`, `Number()`, `new Date()`, `toISOString()`) |
| `new URL()` / `URLSearchParams` | `app/connectors/urls.py` (WHATWG parser/serialiser for http(s)) |
| `fetch.ts`, `url-guard.ts`, `connectors.ts` `fetchHtml`/`politeClient` | `app/connectors/fetch.py`, `app/connectors/polite.py` |
| `text.ts` | `app/connectors/text.py` |
| `timezone.ts` | `app/connectors/timezone.py` |
| `jsonld.ts` | `app/connectors/jsonld.py` |
| `platforms.ts` | `app/connectors/platforms.py` |
| `meetup-search.ts`, `meetup-series.ts`, `meetup-page.ts` | `app/connectors/meetup.py` |
| `luma-discover.ts`, `luma-page.ts` | `app/connectors/luma.py` |
| `eventbrite.ts` | `app/connectors/eventbrite.py` |
| `fatsoma.ts` | `app/connectors/fatsoma.py` |
| `ical.ts` | `app/connectors/ical.py` (aligned) |
| `connectors.ts` (`CONNECTORS`) | `app/connectors/registry.py` |
| `@/lib/places.ts` | `app/sources/places.py` |
| `repeats.ts` | `app/sources/repeats.py` |
| `classify.ts` | `app/sources/classify.py` |
| `interests.ts` + `@/lib/taxonomy.ts` | `app/sources/interests.py` |
| `duplicates.ts` | `app/sources/duplicates.py` |
| `enrich-plan.ts` | `app/sources/enrich_plan.py` |
| `enrich.ts`, `enrich-meetup.ts`, `enrich-imports.ts` | `app/sources/enrich.py` |
| `cloudinary-import.ts` + `@/lib/cloudinary.ts` (`signParams`) | `app/sources/cloudinary.py` |
| `sync-plan.ts`, `engine.ts` | `app/sources/service.py` |
| (new) | `app/sources/cli.py`: `python -m app.sources.cli preview|refresh` |

## Tasks

1. **Shared infrastructure** (spec §2): `js.py`, `urls.py`, `text.py`, `timezone.py`,
   `jsonld.py`, `fetch.py` (SSRF guard on every hop, 15 s / 5 MB caps, Orbit UA,
   `FetchHttpError.status`), `platforms.py`, `places.py` (country table generated from Node's
   `Intl.DisplayNames`, `KNOWN_CITIES` verbatim).
   *Verify:* `/tmp` golden script runs every text/URL/country/timezone/date helper over every
   production title, summary, description, URL and venue plus all fixtures, in TS and Python,
   and diffs the outputs.
2. **Connectors** (spec §3): meetup-search (+ series + single-event page), luma-discover
   (city feed, category API, single event), eventbrite (organiser/event/search), fatsoma
   (page/event/city/discover), registered in `registry.py`. Connector interface becomes
   `fetch(source, now=...)` and returns `interest_hints` with weights.
   *Verify:* fixture-driven golden scripts compare normalised events field by field.
3. **Engine parity** (spec §4–§6, §9, §13): chunked inserts (200) with row-by-row fallback in
   savepoints, `group_repeats`, auto-category, `tag_interests` (TS taxonomy), `fill_covers`,
   errors kept whole and sliced to 50 on save, a run-wide deadline + blocked flag shared by
   all sources (P4), the trusted flag set at the start of every transaction (P1, via a
   session `after_begin` hook on a dedicated engine session), progressive commits so partial
   work persists as in TS, `retry_details` deletes the details row, and refresh runs as a
   background task (cron + admin endpoints answer 202 at once; one refresh at a time).
   `ADD_ONLY_KINDS` stays: production TS (frontend `origin/master` #83) has it, so spec §13.1
   is superseded.
4. **Enrichment** (§7) for Luma, Meetup, Eventbrite/Fatsoma pages; never throws (P5).
   **Cloudinary cover import** (§8) with its disabled path when keys are unset.
5. **Duplicates** (§9.2): `find_duplicates` + `settle_certain_duplicates` replace the old
   exact-title grouping.
6. **Parity evidence**: on `orbit_w7` (prod copy) run `preview` for the 8 production sources
   live (read-only to platforms, politeness on): fetched / would-insert / would-update (fields) /
   missing. Replay the captured platform responses through the TS connectors and compare the
   normalised events exactly. Then a real `refresh_all_sources` against `orbit_w7` and report
   the `source_runs` rows. Drop `orbit_w7` afterwards.

## Verification commands

- `ruff check app/connectors app/sources` (ruff 0.8 as pinned in `pyproject.toml`).
- `python -m app.sources.cli preview` / `refresh` with `DATABASE_URL=…/orbit_w7`.
- `curl` the cron/admin refresh endpoints on port 8107 (401/403 without credentials, 202 with).
