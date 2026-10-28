"""Organiser profiles, public pages, teams, invites and follows."""

from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import (
    current_user,
    require_org_member,
    require_org_owner,
    require_user,
)
from app.core.pagination import pagination

from . import service
from .schemas import MemberAdd, OrganiserCreate, OrganiserUpdate

router = APIRouter(tags=["organisers"])
organisers_router = APIRouter(prefix="/organisers", tags=["organisers"])
invites_router = APIRouter(prefix="/invites", tags=["organisers"])


@organisers_router.get("")
async def list_my_organisers(
    user: dict = Depends(require_user),
    pg: tuple[int, int] = Depends(pagination),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Organisers the caller belongs to, with their role and status."""
    return await service.page_for_member(db, user["id"], *pg)


@organisers_router.get("/slug-available")
async def slug_available(
    slug: str = Query(min_length=1, max_length=60),
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Whether a slug can still be claimed."""
    return {"available": await service.slug_available(db, slug)}


@organisers_router.post("", status_code=status.HTTP_201_CREATED)
async def create_organiser(
    body: OrganiserCreate,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Apply as an organiser; it starts pending and the caller becomes owner."""
    return await service.create_organiser(db, body.model_dump(), user["id"])


@organisers_router.get("/{slug}")
async def public_organiser(
    slug: str,
    user: Optional[dict] = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Public organiser page: profile, follower count, follow state, upcoming events."""
    page = await service.organiser_page(db, slug, user)
    if page is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Organiser not found")
    return page


@organisers_router.patch("/{organiser_id}")
async def update_organiser(
    organiser_id: UUID,
    body: OrganiserUpdate,
    user: dict = Depends(require_org_member),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Edit the organiser profile; review and verification fields cannot be touched."""
    return await service.update_organiser(
        db, organiser_id, body.model_dump(exclude_unset=True)
    )


@organisers_router.get("/{organiser_id}/events")
async def organiser_events(
    organiser_id: UUID,
    when: Literal["upcoming", "past", "all"] = Query("upcoming"),
    pg: tuple[int, int] = Depends(pagination),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """An organiser's public events with guest counts. `upcoming` (default) is
    published events still to come; `past` and `all` also show cancelled and
    expired ones. Drafts are never listed."""
    return await service.public_events_page(db, organiser_id, *pg, when=when)


@organisers_router.put("/{organiser_id}/follow")
async def follow_organiser(
    organiser_id: UUID,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Follow an organiser."""
    return {"following": await service.set_follow(db, organiser_id, user["id"], True)}


@organisers_router.delete("/{organiser_id}/follow")
async def unfollow_organiser(
    organiser_id: UUID,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Stop following an organiser."""
    return {"following": await service.set_follow(db, organiser_id, user["id"], False)}


@organisers_router.get("/{organiser_id}/team")
async def team(
    organiser_id: UUID,
    user: dict = Depends(require_org_member),
    pg: tuple[int, int] = Depends(pagination),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Everyone on the organiser's team, owners first."""
    members = await service.team(db, organiser_id)
    start = (pg[0] - 1) * pg[1]
    return {
        "items": members[start : start + pg[1]],
        "total": len(members),
        "page": pg[0],
        "page_size": pg[1],
    }


@organisers_router.post("/{organiser_id}/members", status_code=status.HTTP_201_CREATED)
async def add_member(
    organiser_id: UUID,
    body: MemberAdd,
    user: dict = Depends(require_org_owner),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Add an existing Orbit account to the team."""
    return await service.add_member(db, organiser_id, str(body.email), body.role)


@organisers_router.delete(
    "/{organiser_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def remove_member(
    organiser_id: UUID,
    user_id: UUID,
    user: dict = Depends(require_org_owner),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Remove a team member; the last owner cannot be removed."""
    await service.remove_member(db, organiser_id, user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@organisers_router.get("/{organiser_id}/invites")
async def list_invites(
    organiser_id: UUID,
    user: dict = Depends(require_org_owner),
    pg: tuple[int, int] = Depends(pagination),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Invite links for the organiser, newest first."""
    return await service.list_invites(db, organiser_id, *pg)


@organisers_router.post("/{organiser_id}/invites", status_code=status.HTTP_201_CREATED)
async def create_invite(
    organiser_id: UUID,
    user: dict = Depends(require_org_owner),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Create an invite code; the organiser must be approved first."""
    return await service.create_invite(db, organiser_id)


@organisers_router.delete("/invites/{invite_id}")
async def revoke_invite(
    invite_id: UUID,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Turn an invite link off; only the owner of that organiser may do it."""
    return await service.revoke_invite(db, invite_id, user)


@invites_router.get("/{code}")
async def peek_invite(
    code: str,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """What the join page needs to render for a code."""
    return await service.peek_invite(db, code)


@invites_router.post("/{code}/accept")
async def accept_invite(
    code: str,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Join the organiser's team with an invite code."""
    return await service.accept_invite(db, code)


router.include_router(organisers_router)
router.include_router(invites_router)
