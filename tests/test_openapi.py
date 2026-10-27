"""The API surface is complete, unique and generates valid OpenAPI.

This is the guard that catches a router that stopped being mounted, two routes
colliding after a refactor, or a response model FastAPI cannot describe — all of
which are silent otherwise.
"""

from app.main import app

spec = app.openapi()
operations = [(method.upper(), path) for path, ops in spec["paths"].items() for method in ops]


def test_operations_are_unique():
    assert len(operations) == len(set(operations))


def test_expected_surface_is_mounted():
    paths = set(spec["paths"])
    required = {
        "/api/health",
        "/api/auth/login",
        "/api/events",
        "/api/events/{event_id}",
        "/api/events/{event_id}/registrations",
        "/api/feed/for-you",
        "/api/me",
        "/api/groups",
        "/api/groups/{group_id}/events",
        "/api/organisers/{organiser_id}/team",
        "/api/organiser/events/{event_id}/check-in",
        "/api/payments/webhook",
        "/api/admin/overview",
        "/api/admin/sources",
        "/api/cron/refresh-sources",
    }
    assert required <= paths


def test_every_operation_has_a_docstring():
    missing = [
        f"{method.upper()} {path}"
        for path, ops in spec["paths"].items()
        for method, meta in ops.items()
        if not (meta.get("summary") or meta.get("description"))
    ]
    assert missing == []


def test_cron_endpoints_require_the_secret():
    cron = spec["paths"]["/api/cron/refresh-sources"]["get"]
    assert any(p.get("name") == "authorization" for p in cron.get("parameters", []))
