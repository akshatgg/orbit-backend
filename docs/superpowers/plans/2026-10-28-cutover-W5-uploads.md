# W5: Uploads and Cloudinary release

Sub-plan of `2026-10-28-supabase-cutover.md` (section W5, gaps 24–26). One PR to `master` from `feat/cutover-w5-uploads`.

**Goal:** the browser uploads images straight to Cloudinary with a signature from this backend, exactly as it does today with the frontend's `/api/uploads/sign`, and the backend deletes Orbit's own Cloudinary images when the events that used them are hard-deleted.

**Frontend reference (mirror exactly):** `src/app/api/uploads/sign/route.ts`, `src/lib/cloudinary.ts` (`signParams`, `orbitImagePublicId`, `ALLOWED_FORMATS`), `src/lib/cloudinary-release.ts`, `src/lib/storage.ts`.

## Task 1: `POST /uploads/sign` matches the frontend contract

Files: `app/uploads/service.py`, `app/uploads/router.py`, `app/core/security.py`.

- `service.py` holds the Cloudinary helpers:
  - `UPLOAD_KINDS = ("event-covers", "organiser-logos")`, `ALLOWED_FORMATS = "jpg,png,webp"`.
  - `sign_params(params, api_secret)`: port of `signParams` (drop `None`/empty values and `file`, `api_key`, `resource_type`, `cloud_name`; sort by key; `k=v` joined with `&`; SHA-1 hex of that string followed by the secret).
  - `orbit_image_public_id(url, cloud_name)`: port of `orbitImagePublicId`.
  - `sign(kind, user_id)` returns `{cloudName, apiKey, timestamp, signature, folder, allowedFormats, maxBytes}` with `folder = orbit/<kind>/<user_id>`, the signature over `allowed_formats`, `folder`, `timestamp`, and `maxBytes` from `IMAGE_MAX_BYTES[kind]`.
- Router: `require_user` (401 when signed out), body `{"kind": "event-covers" | "organiser-logos"}` (422 otherwise), 503 when Cloudinary is not configured, `Cache-Control: no-store` on the response.
- Delete the unused `sign_upload` in `app/core/security.py` (signed only `folder`/`timestamp`; one signer is enough).

## Task 2: remove the local-disk upload path

Files: `app/uploads/router.py`, `app/uploads/service.py`, `app/uploads/__init__.py`, `app/core/config.py`, `.env.example`, `README.md`.

- Delete `POST /uploads` and `service.save`/`max_bytes`/`MIME_EXTENSIONS`.
- Delete `UPLOAD_DIR` from settings and `.env.example`; fix the comments that promise a local fallback.
- `git grep -n "UPLOAD_DIR\|service.save\|var/uploads"` must return nothing outside this plan. `python-multipart` stays (FastAPI form parsing; harmless, and other workstreams may need it).

## Task 3: `app/uploads/release.py`

- `async def release_images(db: AsyncSession, urls: Iterable[str | None]) -> int`
  1. Returns 0 when Cloudinary is not configured.
  2. Keeps only URLs that `orbit_image_public_id` accepts (Orbit's own `orbit/…` images in this account).
  3. In a SAVEPOINT (so a failure cannot abort the caller's transaction), selects which of those URLs are still referenced by `events.cover_url` or `organisers.logo_url` (plus `groups.cover_url` and `profiles.avatar_url`, to be safe). The query runs in the caller's transaction, so it sees the rows the caller just deleted as gone. If the query fails, keeps everything and returns 0.
  4. Schedules a Cloudinary `image/destroy` (signed `public_id`, `timestamp`, `invalidate=true`) for each unused public id, to run only after the caller's transaction commits. A rollback cancels it, so an image is never destroyed for a delete that did not happen, and the request's transaction is not held open while Cloudinary answers.
  5. Returns how many images were scheduled. Never raises; failures are logged.
- `_destroy(public_id)` is the single network boundary (10 s timeout, counts `result == "ok"`).

## Task 4: call sites (one call after each hard delete)

- `app/studio/service.py` `delete_event`: `await release_images(db, [event["cover_url"]])` after the delete.
- `app/admin/router.py` `admin_delete_event`: `await release_images(db, [event.cover_url])` after the delete.
- `app/admin/router.py` `admin_bulk_delete`: read the covers of `body.ids` before the delete, `await release_images(db, covers)` after it.
- Source delete with hard removal is W6's.

## Task 5: verify (database `orbit_w5`, port 8105)

1. `ruff check app`.
2. Cloudinary keys unset: `POST /api/uploads/sign` → 401 signed out, 503 signed in; `POST /api/uploads` → 404/405 (route gone).
3. Dummy Cloudinary values set: sign response shape; recompute the signature independently in a scratch script under `/tmp`; bad kind → 422.
4. Release, with `_destroy` stubbed in a `/tmp` script against `orbit_w5`: an image shared with another event is kept, one used by an organiser logo is kept, an unshared one is destroyed only after commit, foreign/non-`orbit/` URLs ignored, a rolled-back delete destroys nothing.
5. The three call sites over HTTP, with uvicorn started from a `/tmp` wrapper that stubs `_destroy` (no Cloudinary traffic): each returns 204/200, the events are gone, and only the unshared covers reach the stub, after the commit. Unauthorised callers get 401/403.
6. Push, open the PR with the evidence, stop uvicorn, drop `orbit_w5`.
