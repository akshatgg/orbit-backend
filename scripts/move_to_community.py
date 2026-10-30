#!/usr/bin/env python3
"""Move the launch communities into one community, ORBIT, as its channels. Never runs by itself.

    DATABASE_URL=postgresql://... python scripts/move_to_community.py --dry-run
    DATABASE_URL=postgresql://... python scripts/move_to_community.py
    DATABASE_URL=postgresql://... python scripts/move_to_community.py --undo

Moving, in one transaction:

1. Creates ORBIT (`orbit`, public, auto_join), its Announcements channel
   (`orbit-announcements`, moderators only, auto_join) and General (`orbit-general`, open to
   post, opt-in). The creator (as in seed_communities.py) is an admin of all three.
2. Makes each community in seed_communities.py a channel of ORBIT, moderators only, with
   `last_autopost_at` set to now so the daily auto-post doesn't flood it. Any missing is
   reported and skipped; other communities aren't touched.
3. Puts every user in ORBIT and Announcements. Channel members keep their channel role.
   Nobody is notified.
4. Copies each community's announcements into its channel's chat as text messages, keeping
   author and time. The originals stay.

Running it again changes nothing. `--dry-run` does all of this and rolls it back, so the plan
it prints is exact.

`--undo` turns those communities back. A community has no chat, so their conversations go,
with anything posted in them since the move (it says how much). ORBIT, Announcements and General
go too, unless another channel has been created in ORBIT, in which case they stay and it says so.
Files that were attached to deleted messages stay on Cloudinary: this script doesn't touch it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from seed_communities import COMMUNITIES, find_creator  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from app.core.database import get_sync_database_url  # noqa: E402
from app.messaging.links import domain_of, extract_urls  # noqa: E402

ORBIT = "orbit"
ANNOUNCEMENTS = "orbit-announcements"
GENERAL = "orbit-general"
MOVED_SLUGS = [slug for slug, *_ in COMMUNITIES]


def ensure_group(conn, *, slug: str, expect_parent: str | None, **fields) -> tuple[str, bool]:
    """The group with this slug, created if missing. Exits if the slug is someone else's."""
    row = conn.execute(
        text("select id, kind, parent_id, auto_join, post_policy from public.groups where slug = :s"),
        {"s": slug},
    ).first()
    if row:
        expected_kind = "channel" if expect_parent else "community"
        same_parent = str(row.parent_id) == expect_parent if expect_parent else row.parent_id is None
        if row.kind != expected_kind or not same_parent or row.auto_join != fields["auto_join"]:
            sys.exit(f"The slug {slug!r} is already in use by another group. Nothing was changed.")
        return str(row.id), False
    group_id = conn.execute(
        text(
            """
            insert into public.groups
              (kind, parent_id, name, slug, description, visibility, post_policy,
               is_announcements, auto_join, allow_member_shares, created_by)
            values (:kind, cast(:parent as uuid), :name, :slug, :description, 'public', :post_policy,
                    :is_announcements, :auto_join, :allow_member_shares, :creator)
            returning id
            """
        ),
        {
            "kind": "channel" if expect_parent else "community",
            "parent": expect_parent,
            "slug": slug,
            **fields,
        },
    ).scalar_one()
    return str(group_id), True


def ensure_conversation(conn, group_id: str) -> None:
    conn.execute(
        text(
            "insert into public.conversations (kind, group_id, created_by)"
            " select 'channel', g.id, g.created_by from public.groups g where g.id = :g"
            " on conflict (group_id) do nothing"
        ),
        {"g": group_id},
    )


def copy_announcements(conn, group_id: str) -> int:
    """Into the channel's chat, once: a copy is recognised by (conversation, sender, time)."""
    copied = conn.execute(
        text(
            """
            with c as (select id from public.conversations where group_id = :g)
            insert into public.messages (conversation_id, sender_id, body, kind, edited_at, created_at)
            select c.id, a.author_id, a.body, 'text', a.edited_at, a.created_at
              from public.group_announcements a
              cross join c
             where a.group_id = :g
               and not exists (
                 select 1 from public.messages m
                  where m.conversation_id = c.id and m.created_at = a.created_at
                    and m.sender_id is not distinct from a.author_id
               )
            returning id, conversation_id, body, created_at
            """
        ),
        {"g": group_id},
    ).all()
    for row in copied:
        for url in extract_urls(row.body):
            conn.execute(
                text(
                    """
                    insert into public.message_links
                      (message_id, conversation_id, url, domain, fetched_at, created_at)
                    values (:m, :c, :url, :domain, now(), :at)
                    on conflict (message_id, url) do nothing
                    """
                ),
                {
                    "m": row.id,
                    "c": row.conversation_id,
                    "url": url,
                    "domain": domain_of(url),
                    "at": row.created_at,
                },
            )
    if copied:
        conn.execute(
            text(
                """
                update public.conversations c
                   set last_message_at = greatest(
                     c.last_message_at,
                     (select max(m.created_at) from public.messages m where m.conversation_id = c.id))
                 where c.group_id = :g
                """
            ),
            {"g": group_id},
        )
    return len(copied)


def move(conn, creator_ref: str | None) -> None:
    creator = find_creator(conn, creator_ref)
    shared = {"creator": creator}
    orbit, orbit_new = ensure_group(
        conn,
        slug=ORBIT,
        expect_parent=None,
        name="ORBIT",
        description="Everything happening on ORBIT, in one place.",
        post_policy="everyone",
        is_announcements=False,
        auto_join=True,
        allow_member_shares=False,
        **shared,
    )
    announcements, announcements_new = ensure_group(
        conn,
        slug=ANNOUNCEMENTS,
        expect_parent=orbit,
        name="Announcements",
        description="News from the ORBIT team.",
        post_policy="moderators",
        is_announcements=True,
        auto_join=True,
        allow_member_shares=False,
        **shared,
    )
    general, general_new = ensure_group(
        conn,
        slug=GENERAL,
        expect_parent=orbit,
        name="General",
        description="Talk about anything with the rest of ORBIT.",
        post_policy="everyone",
        is_announcements=False,
        auto_join=False,
        allow_member_shares=True,
        **shared,
    )
    for slug, created in ((ORBIT, orbit_new), (ANNOUNCEMENTS, announcements_new), (GENERAL, general_new)):
        print(f"  {'create' if created else 'exists':<8} {slug}")
    ensure_conversation(conn, announcements)
    ensure_conversation(conn, general)

    existing = {
        row.slug: row
        for row in conn.execute(
            text("select id, slug, kind, parent_id from public.groups where slug = any(:s)"),
            {"s": MOVED_SLUGS},
        )
    }
    channels: list[tuple[str, str]] = []
    moved = 0
    for slug in MOVED_SLUGS:
        row = existing.get(slug)
        if not row:
            print(f"  missing  {slug}: skipped")
        elif row.kind == "community":
            conn.execute(
                text(
                    """
                    update public.groups
                       set kind = 'channel', parent_id = :p, post_policy = 'moderators',
                           last_autopost_at = now(), updated_at = now()
                     where id = :id
                    """
                ),
                {"p": orbit, "id": row.id},
            )
            moved += 1
            print(f"  move     {slug}")
            channels.append((slug, str(row.id)))
        elif str(row.parent_id) == orbit:
            print(f"  moved    {slug}: already a channel of ORBIT")
            channels.append((slug, str(row.id)))
        else:
            print(f"  skipped  {slug}: a channel of another community")

    # Every user covers every channel member too. Membership of the channels themselves is kept.
    joined = {}
    for label, group_id in ((ORBIT, orbit), (ANNOUNCEMENTS, announcements), (GENERAL, general)):
        people = (
            "select u.id from public.users u"
            if group_id != general
            else "select created_by as id from public.groups where id = :g"
        )
        joined[label] = conn.execute(
            text(
                f"""
                insert into public.group_members (group_id, user_id, role)
                select :g, x.id, case when x.id = g.created_by then 'admin' else 'member' end
                  from ({people}) x
                  join public.groups g on g.id = :g
                 where x.id is not null
                on conflict do nothing
                """
            ),
            {"g": group_id},
        ).rowcount
    print(f"  members  +{joined[ORBIT]} in ORBIT, +{joined[ANNOUNCEMENTS]} in Announcements")

    copied = 0
    for _, group_id in channels:
        ensure_conversation(conn, group_id)
        copied += copy_announcements(conn, group_id)
    print(f"  copied   {copied} announcements into channel chats")
    print(f"Moved {moved} communities; {len(channels)} channels in ORBIT from the launch list.")


def undo(conn) -> None:
    orbit = conn.execute(
        text("select id from public.groups where slug = :s and kind = 'community' and auto_join"),
        {"s": ORBIT},
    ).scalar()
    if not orbit:
        print("Nothing to undo: there is no ORBIT community.")
        return
    orbit = str(orbit)
    children = conn.execute(
        text("select id, slug from public.groups where parent_id = :p"), {"p": orbit}
    ).all()
    moved = [c for c in children if c.slug in MOVED_SLUGS]
    moved_ids = [str(c.id) for c in moved]
    others = [c.slug for c in children if c.slug not in MOVED_SLUGS and c.slug not in (ANNOUNCEMENTS, GENERAL)]

    posted = conn.execute(
        text(
            """
            select count(*) from public.messages m
              join public.conversations c on c.id = m.conversation_id
             where c.group_id = any(cast(:ids as uuid[]))
               and not exists (
                 select 1 from public.group_announcements a
                  where a.group_id = c.group_id and a.created_at = m.created_at
                    and a.author_id is not distinct from m.sender_id
               )
            """
        ),
        {"ids": moved_ids},
    ).scalar()
    if posted:
        print(f"  warning  {posted} messages posted in these channels since the move go with them")
    conn.execute(
        text("delete from public.conversations where group_id = any(cast(:ids as uuid[]))"),
        {"ids": moved_ids},
    )
    conn.execute(
        text(
            """
            update public.groups
               set kind = 'community', parent_id = null, post_policy = 'everyone',
                   last_autopost_at = null, updated_at = now()
             where id = any(cast(:ids as uuid[]))
            """
        ),
        {"ids": moved_ids},
    )
    for slug in (c.slug for c in moved):
        print(f"  restore  {slug}")

    if others:
        print(
            "  kept     ORBIT, Announcements and General: other channels still depend on them"
            f" ({', '.join(sorted(others))})"
        )
        return
    gone = conn.execute(
        text(
            """
            select count(*) from public.messages m
              join public.conversations c on c.id = m.conversation_id
              join public.groups g on g.id = c.group_id
             where g.parent_id = :p
            """
        ),
        {"p": orbit},
    ).scalar()
    conn.execute(text("delete from public.groups where parent_id = :p"), {"p": orbit})
    conn.execute(text("delete from public.groups where id = :p"), {"p": orbit})
    print(f"  remove   ORBIT, Announcements and General ({gone} messages in them)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    parser.add_argument("--undo", action="store_true", help="reverse the move")
    parser.add_argument("--creator", help="user id or email of the admin who owns ORBIT")
    args = parser.parse_args()

    engine = create_engine(get_sync_database_url())
    with engine.connect() as conn:
        transaction = conn.begin()
        if args.undo:
            undo(conn)
        else:
            move(conn, args.creator)
        if args.dry_run:
            transaction.rollback()
            print("Dry run: nothing was written.")
        else:
            transaction.commit()


if __name__ == "__main__":
    main()
