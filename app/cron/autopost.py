"""The daily auto-post: new events into their channels' chats, right after the import.

For each channel with categories, the events that match it (`MATCHED`, `EVENT_VISIBLE`,
upcoming) and were created since its `last_autopost_at`: up to 5 soonest as event messages,
then one system message "and N more new events" if there are more. A channel that has never
been looked at starts from now, so nothing floods; the move script sets it for the same reason.

Each Announcements channel then gets one summary: how many new events arrived per category, with
a link for each (the category's channel, else its search). Nothing new, no summary.

Then one bell notification per person per run, however many channels they're in; no email and
no push. Everything is set-based, in one transaction: a second run finds nothing new, and a
failure leaves no half-posted channels and never touches the import.
"""

import json
import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.groups.service import EVENT_VISIBLE, UPCOMING

logger = logging.getLogger(__name__)

MAX_POSTS_PER_CHANNEL = 5


def _new_events(n: int) -> str:
    return f"{n} new event{'' if n == 1 else 's'}"


def _summary_href(r: Any) -> str:
    if r["channel_slug"]:
        return f"/communities/{r['parent_slug']}/{r['channel_slug']}"
    if r["slug"]:
        return f"/search?category={r['slug']}"
    return "/search"


def _more_line(n: int) -> str:
    return f"and {n} more new event{'' if n == 1 else 's'}"


async def post_new_events(db: AsyncSession) -> dict[str, int]:
    """One run, in the caller's transaction. A fixed number of statements."""
    rows = (
        await db.execute(
            text(
                f"""
                select ch.id as group_id, x.event_id, x.rn, x.total
                  from public.groups ch
                  left join public.conversations c on c.group_id = ch.id
                  cross join lateral (
                    select e.id as event_id,
                           row_number() over (order by e.starts_at, e.id) as rn,
                           count(*) over () as total
                      from public.events e
                      left join public.organisers o on o.id = e.organiser_id
                     where e.category_id = any(ch.category_ids)
                       and e.status = 'published' and e.duplicate_of is null
                       and (ch.university is null or lower(e.university) = lower(ch.university))
                       and {UPCOMING} and {EVENT_VISIBLE}
                       and e.created_at > coalesce(ch.last_autopost_at, now())
                       and e.created_at <= now()
                       and not exists (
                         select 1 from public.messages m
                          where m.conversation_id = c.id and m.event_id = e.id and m.kind = 'event'
                       )
                  ) x
                 where ch.kind = 'channel' and cardinality(ch.category_ids) > 0
                """
            )
        )
    ).mappings().all()

    posted = [r for r in rows if r["rn"] <= MAX_POSTS_PER_CHANNEL]
    more = {
        str(r["group_id"]): int(r["total"]) - MAX_POSTS_PER_CHANNEL
        for r in rows
        if r["rn"] == 1 and r["total"] > MAX_POSTS_PER_CHANNEL
    }
    notified = 0
    if rows:
        groups = list({str(r["group_id"]) for r in posted})
        await db.execute(
            text(
                """
                insert into public.conversations (kind, group_id, created_by)
                select 'channel', g.id, g.created_by
                  from public.groups g where g.id = any(cast(:ids as uuid[]))
                on conflict (group_id) do nothing
                """
            ),
            {"ids": groups},
        )
        # A millisecond apart, so the thread reads soonest first with the "more" line last.
        await db.execute(
            text(
                """
                insert into public.messages (conversation_id, body, kind, event_id, created_at)
                select c.id, '', 'event', x.event_id, now() + x.rn * interval '1 millisecond'
                  from unnest(cast(:groups as uuid[]), cast(:events as uuid[]), cast(:rns as int[]))
                         as x(group_id, event_id, rn)
                  join public.conversations c on c.group_id = x.group_id
                on conflict (conversation_id, event_id) where kind = 'event' do nothing
                """
            ),
            {
                "groups": [str(r["group_id"]) for r in posted],
                "events": [str(r["event_id"]) for r in posted],
                "rns": [int(r["rn"]) for r in posted],
            },
        )
        if more:
            await db.execute(
                text(
                    """
                    insert into public.messages (conversation_id, body, kind, created_at)
                    select c.id, x.body, 'system', now() + :last * interval '1 millisecond'
                      from unnest(cast(:groups as uuid[]), cast(:bodies as text[])) as x(group_id, body)
                      join public.conversations c on c.group_id = x.group_id
                    """
                ),
                {
                    "groups": list(more),
                    "bodies": [_more_line(n) for n in more.values()],
                    "last": MAX_POSTS_PER_CHANNEL + 1,
                },
            )
        await db.execute(
            text(
                """
                update public.conversations
                   set last_message_at = greatest(last_message_at, now() + :last * interval '1 millisecond')
                 where group_id = any(cast(:ids as uuid[]))
                """
            ),
            {"ids": groups, "last": MAX_POSTS_PER_CHANNEL + 1},
        )
        notified = (
            await db.execute(
                text(
                    """
                    with per_person as (
                      select m.user_id, count(distinct x.event_id) as n, min(parent.slug) as slug
                        from unnest(cast(:groups as uuid[]), cast(:events as uuid[])) as x(group_id, event_id)
                        join public.group_members m on m.group_id = x.group_id
                        join public.groups ch on ch.id = x.group_id
                        join public.groups parent on parent.id = ch.parent_id
                       group by m.user_id
                    ), sent as (
                      insert into public.notifications (user_id, kind, title, link, group_id)
                      select p.user_id, 'event_digest',
                             p.n || case when p.n = 1 then ' new event' else ' new events' end
                               || ' in your channels',
                             '/communities/' || p.slug, g.id
                        from per_person p
                        join public.groups g on g.slug = p.slug
                      returning 1
                    )
                    select count(*) from sent
                    """
                ),
                {
                    "groups": [str(r["group_id"]) for r in rows],
                    "events": [str(r["event_id"]) for r in rows],
                },
            )
        ).scalar() or 0

    channels = (
        await db.execute(
            text(
                "update public.groups set last_autopost_at = now()"
                " where kind = 'channel' and cardinality(category_ids) > 0"
            )
        )
    ).rowcount
    return {
        "channels": channels,
        "channels_posted": len({str(r["group_id"]) for r in posted}),
        "event_messages": len(posted),
        "more_lines": len(more),
        "notified": int(notified),
    }


async def post_summaries(db: AsyncSession) -> dict[str, int]:
    """One summary per Announcements channel, in the caller's transaction. A fixed number of
    statements. Runs after `post_new_events`: both bells share the transaction's now(), which is
    how someone who already got one is left out."""
    rows = (
        await db.execute(
            text(
                f"""
                select a.id as group_id, parent.slug as parent_slug, cat.name, cat.slug, count(*) as n,
                       (array_agg(e.cover_url order by e.starts_at, e.id)
                          filter (where e.cover_url is not null))[1] as cover_url,
                       (select ch.slug from public.groups ch
                         where ch.parent_id = a.parent_id and ch.kind = 'channel'
                           and ch.visibility = 'public' and e.category_id = any(ch.category_ids)
                         order by ch.created_at limit 1) as channel_slug
                  from public.groups a
                  join public.groups parent on parent.id = a.parent_id
                  join public.events e
                    on e.created_at > coalesce(a.last_autopost_at, now()) and e.created_at <= now()
                  left join public.organisers o on o.id = e.organiser_id
                  left join public.categories cat on cat.id = e.category_id
                 where a.is_announcements
                   and e.status = 'published' and e.duplicate_of is null
                   and (parent.university is null or lower(e.university) = lower(parent.university))
                   and {UPCOMING} and {EVENT_VISIBLE}
                 group by a.id, a.parent_id, parent.slug, cat.name, cat.slug, e.category_id
                 order by n desc, cat.name
                """
            )
        )
    ).mappings().all()

    by_group: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_group.setdefault(str(r["group_id"]), []).append(
            {
                "name": r["name"] or "Other",
                "count": int(r["n"]),
                "cover_url": r["cover_url"],
                "href": _summary_href(r),
            }
        )
    notified = 0
    if by_group:
        groups = list(by_group)
        totals = [sum(c["count"] for c in by_group[g]) for g in groups]
        await db.execute(
            text(
                """
                insert into public.conversations (kind, group_id, created_by)
                select 'channel', g.id, g.created_by
                  from public.groups g where g.id = any(cast(:ids as uuid[]))
                on conflict (group_id) do nothing
                """
            ),
            {"ids": groups},
        )
        await db.execute(
            text(
                """
                insert into public.messages (conversation_id, body, kind, data)
                select c.id, x.body, 'summary', cast(x.data as jsonb)
                  from unnest(cast(:groups as uuid[]), cast(:bodies as text[]), cast(:data as text[]))
                         as x(group_id, body, data)
                  join public.conversations c on c.group_id = x.group_id
                """
            ),
            {
                "groups": groups,
                "bodies": [_new_events(n) for n in totals],
                "data": [
                    json.dumps({"total": n, "categories": by_group[g]})
                    for g, n in zip(groups, totals)
                ],
            },
        )
        await db.execute(
            text(
                "update public.conversations set last_message_at = now()"
                " where group_id = any(cast(:ids as uuid[]))"
            ),
            {"ids": groups},
        )
        notified = (
            await db.execute(
                text(
                    """
                    with sent as (
                      insert into public.notifications (user_id, kind, title, link, group_id)
                      select m.user_id, 'event_digest', x.title,
                             '/communities/' || parent.slug || '/' || a.slug, parent.id
                        from unnest(cast(:groups as uuid[]), cast(:titles as text[])) as x(group_id, title)
                        join public.groups a on a.id = x.group_id
                        join public.groups parent on parent.id = a.parent_id
                        join public.group_members m on m.group_id = a.id
                       where not exists (
                         select 1 from public.notifications n
                          where n.user_id = m.user_id and n.kind = 'event_digest' and n.created_at = now()
                       )
                      returning 1
                    )
                    select count(*) from sent
                    """
                ),
                {"groups": groups, "titles": [f"{_new_events(n)} on ORBIT" for n in totals]},
            )
        ).scalar() or 0
    await db.execute(text("update public.groups set last_autopost_at = now() where is_announcements"))
    return {"summaries": len(by_group), "summary_notified": int(notified)}


async def run_autopost() -> dict[str, Any]:
    """One run in its own transaction. Never raises: a failure is logged and reported back."""
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("select set_config('app.trusted', 'on', true)"))
            summary = await post_new_events(db)
            summary |= await post_summaries(db)
            await db.commit()
        logger.info("[cron/autopost] %s", summary)
        return summary
    except Exception as exc:  # noqa: BLE001 - must not fail the import
        logger.exception("[cron/autopost] failed")
        return {"error": type(exc).__name__}
