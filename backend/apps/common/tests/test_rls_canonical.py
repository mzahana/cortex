"""Canonical RLS visibility-differential test (T0.8).

This is the copy-able template T0.9's cross-tenant acceptance test should be
modeled on. Kept in its own module (not `test_smoke.py`) because it needs
`transaction=True` — real commits, never the rolled-back `db` fixture — see
BOTH traps documented in `conftest.py`'s module docstring and
`config/settings/test.py`'s RLS note:

  - Trap 1 (owner-role bypass): every read below goes through
    `app_role_connection` (the non-superuser, RLS-subject `lms_app` role),
    never the owner-role Django ORM connection that wrote the rows.
  - Trap 2 (transaction-isolation false-pass): `transaction=True` on this test
    plus `app_role_connection`'s own `transactional_db` dependency make the
    writes below REAL commits, so `app_role_connection` can see the row AT
    ALL — only then does flipping the GUC isolate RLS as the sole cause of
    any visibility change that follows.
"""

from __future__ import annotations

import pytest

from apps.accounts.models import User
from apps.tenancy.models import Tenant
from conftest import set_app_role_tenant


@pytest.mark.django_db(transaction=True)
def test_rls_visibility_differential_on_committed_data(app_role_connection):
    """A SAME committed row, visible or invisible SOLELY based on which
    tenant `app.current_tenant` is set to on the `lms_app` connection.

    Includes negative controls: if RLS were disabled or misconfigured, the
    "different tenant" and "no tenant" assertions below would fail (the row
    would still be visible) — that dependency is what proves this test is
    actually exercising RLS, not transaction isolation or the owner bypass.
    """
    tenant_a = Tenant.objects.create(name="RLS Tenant A", slug="rls-tenant-a")
    tenant_b = Tenant.objects.create(name="RLS Tenant B", slug="rls-tenant-b")
    user_a = User.all_objects.create(tenant=tenant_a, email="a@rls.test", name="A")

    with app_role_connection.cursor() as cur:
        # Positive: GUC set to the row's OWN tenant -> visible.
        set_app_role_tenant(app_role_connection, tenant_a.id)
        cur.execute("SELECT id FROM accounts_user WHERE id = %s", [user_a.id])
        assert cur.fetchone() is not None, (
            "RLS hid a row from its OWN tenant's GUC -- the policy predicate "
            "is broken, not just strict."
        )

        # Negative control #1 (the one that actually depends on RLS): GUC
        # flipped to a DIFFERENT tenant -> the SAME committed row must become
        # invisible. If RLS were disabled/misconfigured this row would still
        # be returned and this assertion would fail.
        set_app_role_tenant(app_role_connection, tenant_b.id)
        cur.execute("SELECT id FROM accounts_user WHERE id = %s", [user_a.id])
        assert cur.fetchone() is None, (
            "RLS did NOT block a cross-tenant SELECT -- tenant B's GUC could " "see tenant A's row."
        )

        # Negative control #2: GUC cleared entirely -> fail-closed, still invisible.
        set_app_role_tenant(app_role_connection, None)
        cur.execute("SELECT id FROM accounts_user WHERE id = %s", [user_a.id])
        assert cur.fetchone() is None, "RLS did NOT fail closed with no tenant in context."

        # Back to the owning tenant -> visible again, proving the GUC (not a
        # one-way side effect of the earlier queries/connection state) is
        # what controls visibility.
        set_app_role_tenant(app_role_connection, tenant_a.id)
        cur.execute("SELECT id FROM accounts_user WHERE id = %s", [user_a.id])
        assert cur.fetchone() is not None
