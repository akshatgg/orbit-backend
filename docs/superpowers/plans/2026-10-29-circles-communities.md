# Orbit Circles & Communities — plan

Source: "Orbit | New Features" brief (Circles = priority 1, Communities = priority 2).
Principle: extend Orbit without touching the main event feed.

## Where we start

The backend already has most of the data model and API, the frontend has none of it.

- `app/groups/` (models, schemas, router) is mounted at `/api/groups`; its tables come from `0001_baseline`.
  - `groups` — `kind` circle|community, name, slug, description, `category_id`, `university`, `visibility` private|public|approval, cover, created_by. Constraint: a circle is always private.
  - `group_members` — role admin|member.
  - `group_invites` — to a user, or a shareable code; status pending|accepted|declined|revoked; expiry.
  - `group_events` — (group, event) PK, `shared_by` → one share per group, never per friend.
  - `group_event_interests` — interested|going.
  - `group_announcements` — body ≤ 1000 chars.
- Endpoints: list/create/get/patch/delete group, join/leave, members, invites (create, list, respond, by code), share/unshare event, interest put/delete/get, announcements get/post.
- Auth (`app/core/deps.py`): `require_user`, `require_admin` (role admin|super_admin) — enough for both features. Communities are already admin-only to create.
- Events already carry `external_url` / `external_label` (the original registration link) and `category_id`; `user_events.kind = 'saved'` is the existing save.
- Notifications: web push + email only (`app/notifications/channels.py`). No in-app inbox. `notification_log` requires an `event_id`, so it can't record invite notifications.

## Answers to the brief's five questions

1. **Buildable on the current codebase/database:** yes — all of Circles and the Communities MVP. Data model and most endpoints exist; work is bug fixes, two small backend additions, and the whole frontend.
2. **Accounts/auth:** yes. Every user has a profile (name, avatar, university); sessions and admin roles exist. No usernames, so invites go by email or invite link.
3. **Database changes:** the group tables already exist. New: a `notifications` table (in-app inbox) and a partial unique index so one person can't hold two pending invites to the same group. One Alembic revision.
4. **Notifications within Orbit:** not today. Add an in-app inbox (bell + unread count) and also send push/email for invites using the existing senders.
5. **Effort:** ~8–9 developer days end to end (breakdown below). No new external services; email needs the real Resend key, push needs VAPID keys (already stored).

## Phase 0 — fix the existing groups API (backend, ~1 day)

Bugs found in `app/groups/router.py`:

| # | Problem | Fix |
|---|---------|-----|
| 1 | `create_group` turns a circle's `private` into `approval`, violating `groups_circle_is_private` → **creating a circle fails** | Only remap for communities; circles stay `private` |
| 2 | Join request stores the requester as `invited_user_id`, and `respond_invite` lets the invitee accept → **requester approves themself** | Add `kind` (invite\|request) to the invite row, or mark requests with `invited_by = requester`; requests can only be answered by a group admin, invites only by the invitee |
| 3 | `respond_invite` doesn't check status is `pending` or the invite isn't expired; admin can accept on the invitee's behalf | Require pending + unexpired; only the right party may respond |
| 4 | `list_group_events` skips the membership check when signed out → **private circle events readable anonymously** | Signed-out users only see public communities; circles/private require membership |
| 5 | No "my invites" endpoint | `GET /api/groups/invites/mine` (pending invites to me, with group name and inviter) |
| 6 | Duplicate invites/requests possible | Partial unique index `(group_id, invited_user_id) where status='pending'`, `on conflict do nothing` |
| 7 | `share_event` accepts draft/unpublished events | Only published, non-expired events |

Also: `GET /api/groups?mine=1&kind=circle` for "my circles" (check the list endpoint already filters this; add if not), and member removal by admin (`DELETE /{id}/members/{user_id}`) if missing.

## Phase 1 — in-app notifications (backend + bell, ~1.5 days)

- Table `notifications`: id, user_id, kind (`circle_invite`, `join_request`, `request_approved`, `event_shared`, `announcement`), title, body, link, actor_id, group_id, read_at, created_at; index (user_id, read_at, created_at desc).
- `app/notifications/inbox.py`: `notify(db, user_ids, kind, …)` inserts rows and, for invites only, fires `send_push` / `send_email` (existing helpers) respecting the user's notification preferences.
- Endpoints: `GET /api/notifications?unread=1`, `POST /api/notifications/{id}/read`, `POST /api/notifications/read-all`, unread count in the same list response.
- Triggers: invite created → invitee; join request → group admins; request approved → requester; event shared to a circle → other members (in-app only, no email spam); announcement → community members (in-app only).
- Frontend: bell icon with unread badge in `DesktopHeader` / `TopBar`, dropdown list; invite items have Accept / Decline inline.

## Phase 2 — Circles frontend (~3 days)

Pages (under `src/app/(main)/`, server components + server actions like `src/lib/actions/user-events.ts`):

- `/circles` — my circles (name, member avatars, count of upcoming shared events), pending invites at the top, "New circle" form.
- `/circles/[id]` — upcoming shared events (card + "shared by X", registration link button, "I'm interested" / "Count me in" toggle, avatars of interested members); past events collapsed; members tab; admin controls (rename, invite by email, copy invite link, remove member, delete circle); everyone can leave.
- `/join/circle/[code]` — accept an invite link (mirror the existing `src/app/join/[code]` organiser flow).
- **Share with Circle**: button beside `ShareButton`/`SaveButton` on `src/app/events/[id]/page.tsx:137`, and in the `actions` slot of `EventCard`. Opens a sheet listing my circles (already-shared ones ticked), plus "Create a circle". Signed-out users get the sign-in prompt.
- Nav: "Circles" in `DesktopHeader` LINKS; on mobile reach it from the profile tab / top bar rather than adding a sixth bottom tab. The home feed is untouched.

Done when: five test accounts can create a circle, invite by email and link, accept/decline, share an event once, each mark interest, see each other's interest, open the registration link, and the creator can rename and remove members.

## Phase 3 — Communities (~3 days)

Backend (~0.5 day):
- Community feed = events shared into it **plus** upcoming published events matching its `category_id` (and `university`, if set) — linked, never duplicated. Paginated, soonest first.
- Directory: `GET /api/groups?kind=community&category=&university=&q=` with member counts and "joined" flag.
- Admin: allow `require_admin` to create/edit/delete communities and appoint moderators (members with role admin).
- Sharing to a community: members can share if the community allows it (`allow_member_shares` boolean, default true — the one small column addition), otherwise admins only.

Frontend (~2.5 days):
- `/communities` — directory with category chips and a university filter, joined communities first.
- `/communities/[slug]` — name, description, category, member count + member list, Join / Leave / Request to join (approval), announcements (admins post, members read), event feed with Save and registration link.
- "Share with Community" in the same share sheet, under a second heading.
- `/admin/communities` — list/create/edit/delete, set visibility, approve join requests, following `src/app/admin/categories`.
- Seed: the eight communities from the brief (Entrepreneurship & Startups, Consulting & Strategy, Technology & AI, Finance & Investment, Marketing & Branding, Sports & Fitness, Arts & Culture, Social Events & Weekend Activities), each mapped to its closest existing category.

Done when: a student can browse/filter the directory, join a public community, request a private one and be approved, see matching events, save one, open its registration link, and read an announcement.

## Phase 4 — Messaging: Circle chat + direct messages (~2.5 days)

Added on request (the brief defers chat/DMs; kept deliberately simple).

- **Circle chat:** one conversation per Circle, members only. Leaving/removal drops access immediately (membership is checked against `group_members`, not copied).
- **Direct messages:** one-to-one between two users who share at least one Circle or Community (stops cold spam; there is no open user search). Started from a member list ("Message").
- **Delivery:** polling — the open thread polls `?after=` every ~4 s, the badge every ~30 s. No websockets.
- **Unread:** per-user `last_read_at` per conversation; total unread shows as a badge on a Messages icon in the nav. Messages do not go into the notifications inbox (too noisy). A DM sends a web push only if the recipient has no unread push from that conversation in the last 10 minutes.
- **Safety:** body 1–2000 chars, ~20 messages/minute per user, sender can delete their own message (soft delete → "Message deleted"), a user can block another (block hides and prevents DMs both ways).

## API contract (backend ↔ frontend)

All under `/api`, JSON, session cookie / Bearer auth. User objects are `{id, full_name, avatar_url, university}` (never email except to the user themself).

Groups (existing routes, fixed):
- `GET /groups?kind=circle|community&mine=1&category=<slug|id>&university=&q=&page=` → `{items:[{id,kind,name,slug,description,visibility,category_id,category_name,university,cover_url,member_count,my_role|null,upcoming_event_count}], total, page, page_size}`
- `POST /groups` `{kind,name,description?,visibility?,category_id?,university?,cover_url?}` → group (community: admins only)
- `GET /groups/{id_or_slug}` → group + `my_role`, `pending_request` (bool)
- `PATCH /groups/{id}`, `DELETE /groups/{id}` (group admin; community also Orbit admin)
- `POST /groups/{id}/join` → `{status:"joined"|"requested"}`; `POST /groups/{id}/leave`
- `GET /groups/{id}/members` → `{items:[{user,role,joined_at}]}`; `DELETE /groups/{id}/members/{user_id}`; `PATCH /groups/{id}/members/{user_id}` `{role}`
- `POST /groups/{id}/invites` `{email}` → invite (circle: any member; community: admins); `GET /groups/{id}/invites` (admins: pending invites + join requests, each with `kind: invite|request`)
- `POST /groups/{id}/invites/{invite_id}/respond` `{accept}`
- `GET /groups/invites/mine` → pending invites to me `{items:[{id,group:{id,name,kind},invited_by:user,created_at}]}`
- `POST /groups/{id}/invite-link` → `{code,url_path:"/join/circle/<code>"}`; `GET /groups/invites/{code}` preview; `POST /groups/invites/{code}/accept`
- `GET /groups/{id}/events?when=upcoming|past` → `{items:[{event:{…event card fields, external_url, external_label}, shared_by:user|null, note, shared_at, interested:[user], going:[user], my_status}]}`; communities also include category-matched events with `shared_by:null`
- `POST /groups/{id}/events` `{event_id,note?}`; `DELETE /groups/{id}/events/{event_id}`
- `PUT /groups/{id}/events/{event_id}/interest` `{status}`; `DELETE …/interest`
- `GET /groups/{id}/announcements`; `POST /groups/{id}/announcements` `{body,event_id?}` (admins)
- `GET /groups/shareable?event_id=` → my circles/communities I can share to, each with `already_shared`

Notifications:
- `GET /notifications?unread=1&limit=` → `{items:[{id,kind,title,body,link,actor:user|null,group_id,invite_id,read_at,created_at}], unread_count}`
- `POST /notifications/{id}/read`, `POST /notifications/read-all`

Messages:
- `GET /messages/conversations` → `{items:[{id,kind:"circle"|"direct",group:{id,name}|null,other_user:user|null,last_message:{body,sender_id,created_at}|null,unread_count}], unread_total}`
- `GET /messages/unread-count` → `{unread_total}`
- `POST /messages/direct` `{user_id}` → `{id}` (get-or-create; 403 if no shared group or blocked)
- `GET /groups/{id}/conversation` → `{id}` (get-or-create circle chat; members only)
- `GET /messages/conversations/{id}?after=<iso>&before=<iso>&limit=50` → `{conversation:{…as list item, members:[user]}, items:[{id,sender:user,body,deleted,created_at}]}` oldest→newest
- `POST /messages/conversations/{id}` `{body}` → message
- `POST /messages/conversations/{id}/read`
- `DELETE /messages/{message_id}` (own only)
- `POST /messages/blocks` `{user_id}`; `DELETE /messages/blocks/{user_id}`; `GET /messages/blocks`

## Out of scope (later)

Comments on shared events, discussion forum, advanced moderation, user-created communities, open user search (avoided for now so emails/profiles can't be enumerated), realtime websockets, attachments in messages.

## Effort summary

| Phase | Days |
|-------|------|
| 0 — groups API fixes | 1 |
| 1 — in-app notifications | 1.5 |
| 2 — Circles UI | 3 |
| 3 — Communities | 3 |
| 4 — Circle chat + DMs | 2.5 |
| **Total** | **~11** |

Dependencies: real Resend key for invite emails (in-app and push work without it). Ship order: backend PR (0 + 1 + community feed) → frontend Circles PR → frontend Communities PR. No test files; verify against the running backend and app.
