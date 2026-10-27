"""Test bootstrap: make settings importable without a real database.

Importing `app.main` builds the async engine, but no connection is opened until
a request runs — so these tests need a URL, not a server.
"""

import os

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://orbit:orbit@localhost:5433/orbit_test")
os.environ.setdefault("JWT_SECRET", "test-secret")
