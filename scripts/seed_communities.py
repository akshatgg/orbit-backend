#!/usr/bin/env python3
"""Create Orbit's first eight Communities. Safe to run again: existing slugs are left alone.

    DATABASE_URL=postgresql://... python scripts/seed_communities.py
    DATABASE_URL=postgresql://... python scripts/seed_communities.py --creator someone@example.com

Each community is public, open to every university, and mapped to the closest
existing category (the first slug in its list that exists), so its feed picks
up that category's upcoming events. The creator (default: the earliest
super_admin, else the earliest admin) becomes each community's admin.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text  # noqa: E402

from app.core.database import get_sync_database_url  # noqa: E402

# (slug, name, description, category slugs in order of preference)
COMMUNITIES: list[tuple[str, str, str, list[str]]] = [
    (
        "entrepreneurship-startups",
        "Entrepreneurship & Startups",
        "Founders, builders and the startup-curious: pitch nights, founder talks and hackathons.",
        ["entrepreneurship"],
    ),
    (
        "consulting-strategy",
        "Consulting & Strategy",
        "Case practice, firm presentations and strategy talks for future consultants.",
        ["employer-presentation", "workshop", "talk"],
    ),
    (
        "technology-ai",
        "Technology & AI",
        "Software, data and AI: workshops, hackathons and tech talks.",
        ["workshop", "competition", "talk"],
    ),
    (
        "finance-investment",
        "Finance & Investment",
        "Banking, investing and markets: insight days, careers fairs and finance talks.",
        ["careers-fair", "employer-presentation", "networking"],
    ),
    (
        "marketing-branding",
        "Marketing & Branding",
        "Brand, growth and creative marketing: talks, panels and workshops.",
        ["talk", "workshop"],
    ),
    (
        "sports-fitness",
        "Sports & Fitness",
        "Runs, classes, matches and anything that gets you moving.",
        ["fitness"],
    ),
    (
        "arts-culture",
        "Arts & Culture",
        "Exhibitions, theatre, film and music around campus and the city.",
        ["arts-culture", "music"],
    ),
    (
        "social-weekend",
        "Social Events & Weekend Activities",
        "Socials, trips and things to do at the weekend.",
        ["social", "travel-and-outdoors", "hobbies-and-passion"],
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
    parser.add_argument("--creator", help="user id or email of the admin who owns them")
    args = parser.parse_args()

    engine = create_engine(get_sync_database_url())
    with engine.begin() as conn:
        creator = find_creator(conn, args.creator)
        categories = {
            slug: str(cid)
            for cid, slug in conn.execute(text("select id, slug from public.categories"))
        }
        for slug, name, description, preferred in COMMUNITIES:
            category = next((categories[s] for s in preferred if s in categories), None)
            group_id = conn.execute(
                text(
                    """
                    insert into public.groups
                      (kind, name, slug, description, visibility, category_id, created_by)
                    values ('community', :name, :slug, :description, 'public',
                            cast(:category as uuid), :creator)
                    on conflict (slug) do nothing
                    returning id
                    """
                ),
                {
                    "name": name,
                    "slug": slug,
                    "description": description,
                    "category": category,
                    "creator": creator,
                },
            ).scalar()
            if group_id is None:
                print(f"  exists   {slug}")
                continue
            conn.execute(
                text(
                    "insert into public.group_members (group_id, user_id, role)"
                    " values (:g, :u, 'admin') on conflict do nothing"
                ),
                {"g": group_id, "u": creator},
            )
            mapped = next((s for s in preferred if s in categories), "none")
            print(f"  created  {slug}  (category: {mapped})")


if __name__ == "__main__":
    main()
