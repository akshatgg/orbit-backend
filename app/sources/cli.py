"""Run the importers from a shell (on the VM, or against a copy of production data).

    python -m app.sources.cli preview [SOURCE_ID ...] [--json out.json]
        Fetch each enabled source (or the ones named) and show what a run would change:
        found, would-insert, would-update (by field), unchanged and would-flag. Reads the
        platforms; writes nothing.

    python -m app.sources.cli refresh [--trigger cron|manual]
        A real refresh of every enabled source, then expiry and duplicate detection.
"""

import argparse
import asyncio
import json
from collections import Counter
from typing import Any
from uuid import UUID

from sqlalchemy import select

from app.sources.models import EventSource
from app.sources.service import engine_session, refresh_all_sources, run_source


async def _enabled_sources() -> list[tuple[UUID, str, str]]:
    async with engine_session() as db:
        rows = await db.execute(
            select(EventSource.id, EventSource.name, EventSource.kind)
            .where(EventSource.enabled.is_(True))
            .order_by(EventSource.created_at)
        )
        return [tuple(row) for row in rows.all()]


async def preview(source_ids: list[str], out: str | None) -> None:
    sources = await _enabled_sources()
    if source_ids:
        wanted = {UUID(s) for s in source_ids}
        sources = [s for s in sources if s[0] in wanted]
    report: list[dict[str, Any]] = []
    for source_id, name, kind in sources:
        summary = await run_source(source_id, "manual", dry_run=True)
        plan = summary.get("plan") or {}
        fields = Counter(field for update in plan.get("updates", []) for field in update["fields"])
        row = {
            "source": name, "kind": kind, "status": summary["status"], "found": summary["found"],
            "would_insert": len(plan.get("inserts", [])), "would_update": len(plan.get("updates", [])),
            "update_fields": dict(fields), "unchanged": plan.get("unchanged", 0),
            "would_flag": len(plan.get("flagged", [])), "would_unflag": len(plan.get("unflagged", [])),
            "existing": plan.get("existing", 0), "classified": summary.get("classified", 0),
            "errors": summary["errors"],
        }
        report.append({**row, "updates": plan.get("updates", []), "inserts": plan.get("inserts", [])})
        print(
            f"{name} [{kind}] {summary['status']}: found {row['found']}, would insert {row['would_insert']}, "
            f"update {row['would_update']} {row['update_fields'] or ''}, unchanged {row['unchanged']}, "
            f"flag {row['would_flag']}, errors {len(row['errors'])}",
            flush=True,
        )
    if out:
        with open(out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, default=str)


async def refresh(trigger: str) -> None:
    summary = await refresh_all_sources(trigger)
    print(json.dumps(summary, indent=2, default=str))


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.sources.cli", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("preview", help="show what a run would change, without writing")
    p.add_argument("source_ids", nargs="*")
    p.add_argument("--json", dest="out")
    r = commands.add_parser("refresh", help="run every enabled source now")
    r.add_argument("--trigger", choices=("cron", "manual"), default="manual")
    args = parser.parse_args()
    if args.command == "preview":
        asyncio.run(preview(args.source_ids, args.out))
    else:
        asyncio.run(refresh(args.trigger))


if __name__ == "__main__":
    main()
