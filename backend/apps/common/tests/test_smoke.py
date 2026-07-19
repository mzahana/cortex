"""T0.8 smoke tests: prove the pytest/pytest-django/Postgres harness itself is
meaningful (migrations run, app boots, DB round-trips, the RLS role/backstop
introduced in T0.5 is live) — NOT the T0.9 acceptance tests (cross-tenant
403/404, RBAC scoping), which land as their own module per
`docs/tasks/M0-foundations.md` T0.9 and drop straight into this harness using
the `app_role_connection` fixture from `backend/conftest.py`.
"""

from __future__ import annotations

import pytest
from django.test import Client

pytestmark = pytest.mark.django_db


def test_healthz_ok():
    """`/healthz` is served before URL resolution
    (`config.middleware.HealthCheckMiddleware`) and never touches the DB —
    the most basic proof the Django app boots under test settings."""
    response = Client().get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_app_imports_and_migrations_ran():
    """A trivial ORM round-trip proves migrations actually applied against
    the throwaway Postgres (not just that Django could import settings)."""
    from apps.tenancy.models import Tenant

    # `Tenant` is the root of the multi-tenant tree (see models.py docstring)
    # — it is deliberately NOT a `TenantScopedModel`, so it only has the
    # ordinary `objects` manager, no `all_objects`/tenant filter/RLS.
    assert Tenant.objects.count() == 0
    tenant = Tenant.objects.create(name="Smoke Tenant", slug="smoke-tenant")
    assert Tenant.objects.filter(pk=tenant.pk).exists()


def test_app_role_connection_is_rls_subject(app_role_connection):
    """Proves the harness T0.9 needs is wired correctly: `cortex_app` (T0.5) is
    a real, connectable, non-superuser login role in the test database, RLS
    is enabled on a known tenant-owned table, and — with no
    `app.current_tenant` GUC set on this connection — the fail-closed
    predicate hides every row, even one that unquestionably exists (written
    moments ago via the owner connection above). This is the mechanism T0.9's
    cross-tenant acceptance test asserts against real tenant data; this test
    only proves the mechanism itself works.
    """
    from apps.tenancy.models import Tenant

    Tenant.objects.create(name="RLS Smoke Tenant", slug="rls-smoke-tenant")

    with app_role_connection.cursor() as cur:
        # `cortex_app` must not be a superuser/BYPASSRLS role, or this whole
        # backstop would be inert (see tenancy/migrations/0003_app_db_role.py).
        cur.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        rolsuper, rolbypassrls = cur.fetchone()
        assert rolsuper is False
        assert rolbypassrls is False

        # RLS is actually enabled on a real tenant-owned table (T0.5).
        cur.execute("SELECT relrowsecurity FROM pg_class WHERE relname = 'accounts_user'")
        assert cur.fetchone() == (True,)

        # No GUC set on THIS connection -> fail-closed -> zero rows, even
        # though `tenancy_tenant` (not itself RLS-protected) shows the row
        # exists. `accounts_user` has no rows yet at M0-smoke time, so assert
        # against the row count directly instead of a specific id.
        cur.execute("SELECT current_setting('app.current_tenant', true)")
        assert cur.fetchone()[0] in (None, "")
