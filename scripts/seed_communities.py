#!/usr/bin/env python3
"""Create Orbit's launch Communities. Safe to run again: existing ones are updated, not duplicated.

    DATABASE_URL=postgresql://... python scripts/seed_communities.py
    DATABASE_URL=postgresql://... python scripts/seed_communities.py --creator someone@example.com

Each community is public, open to every university, and linked to every listed
category that exists (a missing slug is skipped with a warning), so its feed
picks up those categories' upcoming events. The first category is its primary
one. On a re-run, a community whose slug already exists gets its categories and
description refreshed; its name, visibility and members are left alone. The
creator (default: the earliest super_admin, else the earliest admin) becomes
each new community's admin.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text  # noqa: E402

from app.core.database import get_sync_database_url  # noqa: E402

# (slug, name, description, category slugs — primary first)
COMMUNITIES: list[tuple[str, str, str, list[str]]] = [
    (
        "careers-networking",
        "Careers & Networking",
        "Careers fairs, employer sessions and networking nights to meet your next team.",
        ["careers-fair", "employer-presentation", "networking"],
    ),
    (
        "sports-fitness",
        "Sports & Fitness",
        "Runs, classes, matches and anything that gets you moving.",
        ["fitness"],
    ),
    (
        "hobbies-socials",
        "Hobbies & Socials",
        "Society meetups, socials and new hobbies to try with good company.",
        ["hobbies-and-passion", "social", "society"],
    ),
    (
        "travel-outdoors",
        "Travel & Outdoors",
        "Day trips, hikes and adventures beyond campus.",
        ["travel-and-outdoors"],
    ),
    (
        "arts-culture-music",
        "Arts, Culture & Music",
        "Gigs, galleries, theatre and everything creative around the city.",
        ["arts-culture", "music"],
    ),
    (
        "workshops-talks",
        "Workshops & Talks",
        "Hands-on workshops, talks and panels to learn something new.",
        ["workshop", "talk"],
    ),
    (
        "startups-entrepreneurship",
        "Startups & Entrepreneurship",
        "Pitch nights, founder talks and competitions for builders and the startup-curious.",
        ["entrepreneurship", "competition"],
    ),
    (
        "sustainability-impact",
        "Sustainability & Impact",
        "Climate, volunteering and projects that make a difference.",
        ["sustainability"],
    ),
]


def find_creator(conn, ref: str | None) -> str:
    if ref:
        try:
            row = conn.execute(
                text("select id from public.profiles where id = :id"), {"id": str(UUID(ref))}
            ).first()
        except ValueError:
            row = conn.execute(
                text("select id from public.profiles where lower(email) = lower(:e)"), {"e": ref}
            ).first()
        if not row:
            sys.exit(f"No profile matches --creator {ref!r}")
        return str(row[0])
    row = conn.execute(
        text(
            "select id from public.profiles where role in ('super_admin', 'admin')"
            " order by (role = 'super_admin') desc, created_at limit 1"
        )
    ).first()
    if not row:
        sys.exit("No admin account found: pass --creator <user id or email>")
    return str(row[0])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--creator", help="user id or email of the admin who owns new ones")
    args = parser.parse_args()

    engine = create_engine(get_sync_database_url())
    with engine.begin() as conn:
        creator = find_creator(conn, args.creator)
        categories = {
            slug: str(cid)
            for cid, slug in conn.execute(text("select id, slug from public.categories"))
        }
        for slug, name, description, wanted in COMMUNITIES:
            linked = [s for s in wanted if s in categories]
            for missing in (s for s in wanted if s not in categories):
                print(f"  warning  {slug}: no category {missing!r}, skipped")
            ids = [categories[s] for s in linked]
            row = conn.execute(
                text(
                    """
                    insert into public.groups
                      (kind, name, slug, description, visibility, category_id, category_ids,
                       created_by)
                    values ('community', :name, :slug, :description, 'public',
                            cast(:primary as uuid), cast(:ids as uuid[]), :creator)
                    on conflict (slug) do update
                      set description = excluded.description,
                          category_id = excluded.category_id,
                          category_ids = excluded.category_ids,
                          updated_at = now()
                    returning id, (xmax = 0) as created
                    """
                ),
                {
                    "name": name,
                    "slug": slug,
                    "description": description,
                    "primary": ids[0] if ids else None,
                    "ids": ids,
                    "creator": creator,
                },
            ).first()
            if row.created:
                conn.execute(
                    text(
                        "insert into public.group_members (group_id, user_id, role)"
                        " values (:g, :u, 'admin') on conflict do nothing"
                    ),
                    {"g": row.id, "u": creator},
                )
            state = "created" if row.created else "updated"
            print(f"  {state:<8} {slug}  (categories: {', '.join(linked) or 'none'})")


if __name__ == "__main__":
    main()
