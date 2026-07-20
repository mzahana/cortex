"""T5.4 — RLS visibility differential on the two T5.1 notifications
tables (`notifications_email_log`, `notifications_notification_pref`).
Mirrors `apps.common.tests.test_rls_canonical`'s canonical template exactly:
a SAME committed row, visible or invisible SOLELY based on which tenant
`app.current_tenant` is set to on the real `cortex_app` role connection.

Both traps from `conftest.py`'s module docstring are avoided the same way:
  - Trap 1 (owner-role bypass): every read below goes through
    `app_role_connection`, never the owner-role Django ORM connection that
    wrote the rows.
  - Trap 2 (transaction-isolation false-pass): `transaction=True` on this
    test plus `app_role_connection`'s own `transactional_db` dependency
    make the writes below REAL commits.
"""

from __future__ import annotations

import pytest

from apps.common.tests.factories import TenantFactory, UserFactory
from apps.notifications.models import EmailLog, NotificationPref
from conftest import set_app_role_tenant


@pytest.mark.django_db(transaction=True)
def test_rls_visibility_differential_on_email_log(app_role_connection):
    tenant_a = TenantFactory()
    tenant_b = TenantFactory()
    user_a = UserFactory(tenant=tenant_a)
    log = EmailLog.all_objects.create(
        tenant=tenant_a,
        user=user_a,
        recipient=user_a.email,
        event_type="reservation_confirmed",
        provider="ConsoleProvider",
        status=EmailLog.Status.SENT,
    )

    with app_role_connection.cursor() as cur:
        # Positive: GUC set to the row's OWN tenant -> visible.
        set_app_role_tenant(app_role_connection, tenant_a.id)
        cur.execute("SELECT id FROM notifications_email_log WHERE id = %s", [log.id])
        assert cur.fetchone() is not None, (
            "RLS hid a row from its OWN tenant's GUC -- the policy predicate "
            "is broken, not just strict."
        )

        # Negative control #1: GUC flipped to a DIFFERENT tenant -> the SAME
        # committed row must become invisible.
        set_app_role_tenant(app_role_connection, tenant_b.id)
        cur.execute("SELECT id FROM notifications_email_log WHERE id = %s", [log.id])
        assert (
            cur.fetchone() is None
        ), "RLS did NOT block a cross-tenant SELECT on notifications_email_log."

        # Negative control #2: GUC cleared entirely -> fail-closed, still invisible.
        set_app_role_tenant(app_role_connection, None)
        cur.execute("SELECT id FROM notifications_email_log WHERE id = %s", [log.id])
        assert cur.fetchone() is None, "RLS did NOT fail closed with no tenant in context."

        # Back to the owning tenant -> visible again.
        set_app_role_tenant(app_role_connection, tenant_a.id)
        cur.execute("SELECT id FROM notifications_email_log WHERE id = %s", [log.id])
        assert cur.fetchone() is not None


@pytest.mark.django_db(transaction=True)
def test_rls_visibility_differential_on_notification_pref(app_role_connection):
    tenant_a = TenantFactory()
    tenant_b = TenantFactory()
    user_a = UserFactory(tenant=tenant_a)
    pref = NotificationPref.all_objects.create(
        tenant=tenant_a,
        user=user_a,
        event_type="low_stock_crossed",
        email_enabled=False,
    )

    with app_role_connection.cursor() as cur:
        set_app_role_tenant(app_role_connection, tenant_a.id)
        cur.execute("SELECT id FROM notifications_notification_pref WHERE id = %s", [pref.id])
        assert cur.fetchone() is not None, (
            "RLS hid a row from its OWN tenant's GUC -- the policy predicate "
            "is broken, not just strict."
        )

        set_app_role_tenant(app_role_connection, tenant_b.id)
        cur.execute("SELECT id FROM notifications_notification_pref WHERE id = %s", [pref.id])
        assert (
            cur.fetchone() is None
        ), "RLS did NOT block a cross-tenant SELECT on notifications_notification_pref."

        set_app_role_tenant(app_role_connection, None)
        cur.execute("SELECT id FROM notifications_notification_pref WHERE id = %s", [pref.id])
        assert cur.fetchone() is None, "RLS did NOT fail closed with no tenant in context."

        set_app_role_tenant(app_role_connection, tenant_a.id)
        cur.execute("SELECT id FROM notifications_notification_pref WHERE id = %s", [pref.id])
        assert cur.fetchone() is not None
