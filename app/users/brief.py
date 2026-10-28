"""The public face of a person: `{id, full_name, avatar_url, university}`.

What other people may see of someone in groups, the inbox and messages. Never
the email address — only the person themself sees that.
"""

from typing import Any, Mapping, Optional


def user_columns(id_expr: str, profile: str, prefix: str) -> str:
    """SELECT columns for one person, read back by `user_brief(row, prefix)`.

    `id_expr` is the column holding their id (kept even if the profile row is
    missing); `profile` is the alias of a `left join public.profiles`.
    """
    return (
        f"{id_expr} as {prefix}id, {profile}.full_name as {prefix}full_name,"
        f" {profile}.avatar_url as {prefix}avatar_url, {profile}.university as {prefix}university"
    )


def user_brief(row: Mapping[str, Any], prefix: str) -> Optional[dict[str, Any]]:
    if row.get(f"{prefix}id") is None:
        return None
    return {
        "id": str(row[f"{prefix}id"]),
        "full_name": row.get(f"{prefix}full_name"),
        "avatar_url": row.get(f"{prefix}avatar_url"),
        "university": row.get(f"{prefix}university"),
    }
