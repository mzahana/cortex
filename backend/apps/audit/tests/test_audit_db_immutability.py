"""T5.4 — DB-level backstop for `AuditLog` append-only-ness (F8 acceptance:
"entries cannot be edited/deleted via the app" -- proved here one level
deeper, at the database itself). Mirrors
`apps.stock.tests.test_stock_db_immutability` exactly: this module proves
the migration's DB-level trigger (`audit_log_forbid_mutation`, `BEFORE
UPDATE OR DELETE ON audit_audit_log`) independently rejects a RAW
UPDATE/DELETE that never goes through the ORM/app at all -- the app layer
never exposing an update/delete path is a necessary but insufficient
guarantee; a stray raw SQL statement (bad script, compromised admin tool,
future code) must still be rejected by Postgres itself.

Uses `app_role_connection` (the real, non-superuser `cortex_app` runtime
role) -- the same role the deployed app actually connects as -- not the
owner-role Django ORM connection. Depends on `transactional_db` (via
`app_role_connection`) so the `AuditLog` row is a REAL commit, not a
same-test-only row hidden behind rolled-back transaction isolation.
"""

from __future__ import annotations

import psycopg
import pytest

from apps.audit.models import AuditLog
from apps.common.tests.factories import TenantFactory, UserFactory
from conftest import set_app_role_tenant


@pytest.mark.django_db(transaction=True)
def test_raw_update_of_an_audit_log_entry_is_rejected_at_the_db(app_role_connection):
    tenant = TenantFactory()
    actor = UserFactory(tenant=tenant)
    entry = AuditLog.all_objects.create(
        tenant=tenant,
        actor=actor,
        action="asset.retire",
        entity_type="asset",
        entity_id="1",
        before={"status": "active"},
        after={"status": "retired"},
    )

    set_app_role_tenant(app_role_connection, tenant.id)
    with app_role_connection.cursor() as cur:
        with pytest.raises(psycopg.errors.RestrictViolation) as exc_info:
            cur.execute(
                "UPDATE audit_audit_log SET action = %s WHERE id = %s",
                ["tampered", entry.id],
            )
        assert "append-only" in str(exc_info.value)
    # The raised exception aborts the current transaction on this
    # connection; roll back before issuing any further statement on it.
    app_role_connection.rollback()

    # The row in the DB is untouched -- verified via a FRESH statement on
    # the same connection (the aborted UPDATE above never committed).
    with app_role_connection.cursor() as cur:
        set_app_role_tenant(app_role_connection, tenant.id)
        cur.execute("SELECT action FROM audit_audit_log WHERE id = %s", [entry.id])
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "asset.retire"


@pytest.mark.django_db(transaction=True)
def test_raw_delete_of_an_audit_log_entry_is_rejected_at_the_db(app_role_connection):
    tenant = TenantFactory()
    actor = UserFactory(tenant=tenant)
    entry = AuditLog.all_objects.create(
        tenant=tenant,
        actor=actor,
        action="stock.adjust",
        entity_type="stock_item",
        entity_id="1",
        before={"quantity_on_hand": 10},
        after={"quantity_on_hand": 8},
    )

    set_app_role_tenant(app_role_connection, tenant.id)
    with app_role_connection.cursor() as cur:
        with pytest.raises(psycopg.errors.RestrictViolation) as exc_info:
            cur.execute("DELETE FROM audit_audit_log WHERE id = %s", [entry.id])
        assert "append-only" in str(exc_info.value)
    app_role_connection.rollback()

    with app_role_connection.cursor() as cur:
        set_app_role_tenant(app_role_connection, tenant.id)
        cur.execute("SELECT id FROM audit_audit_log WHERE id = %s", [entry.id])
        assert cur.fetchone() is not None, "The row must still exist -- the DELETE was rejected."


@pytest.mark.django_db(transaction=True)
def test_a_new_audit_log_entry_is_still_insertable_at_the_db(app_role_connection):
    """Negative control: the trigger only fires on UPDATE/DELETE (`BEFORE
    UPDATE OR DELETE`, no `INSERT` in its event list) -- a plain new entry
    must still be writable at the DB level via the same app-role
    connection. Without this, the two tests above could pass vacuously if
    the trigger (mis-)rejected everything.
    """
    tenant = TenantFactory()
    actor = UserFactory(tenant=tenant)

    set_app_role_tenant(app_role_connection, tenant.id)
    with app_role_connection.cursor() as cur:
        cur.execute(
            """
            INSERT INTO audit_audit_log
                (tenant_id, actor_id, action, entity_type, entity_id, before, after, ip, created_at)
            VALUES (%s, %s, %s, %s, %s, NULL, NULL, NULL, now())
            RETURNING id
            """,
            [tenant.id, actor.id, "role.assign", "user", "1"],
        )
        new_id = cur.fetchone()[0]
    app_role_connection.commit()

    with app_role_connection.cursor() as cur:
        set_app_role_tenant(app_role_connection, tenant.id)
        cur.execute("SELECT action FROM audit_audit_log WHERE id = %s", [new_id])
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "role.assign"
