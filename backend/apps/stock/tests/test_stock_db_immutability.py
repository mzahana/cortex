"""T2.5 — DB-level backstop for `StockTxn` append-only-ness (F3 acceptance:
"a correction is an immutable new row, not an edit"). The app-layer guard
(`StockTxn.save()`/`.delete()` refusing anything but the original INSERT,
proved in `apps.stock.tests.test_stock_models.TestStockTxnAppendOnly`) is
NOT what's being tested here — that only proves the Django ORM path is
guarded. This module proves the T2.3 migration's DB-level trigger
(`stock_txn_forbid_mutation`, `BEFORE UPDATE OR DELETE ON stock_stock_txn`)
independently rejects a RAW UPDATE/DELETE that never goes through the ORM at
all — the actual gap code review flagged (T2.3's own proof of this trigger
was migration-authoring-time only, no test exercises it).

Uses `app_role_connection` (the real, non-superuser `cortex_app` runtime
role), not the owner-role Django ORM connection — table triggers fire
regardless of role, but exercising this via the SAME role the deployed app
actually connects as is the more faithful proof, and keeps this test
immune to the "which connection is doing the checking" confusion the RLS
tests are careful about (`conftest.py`'s module docstring). Depends on
`transactional_db` (via `app_role_connection`) so the `StockTxn` row is a
REAL commit, not a same-test-only row hidden behind rolled-back transaction
isolation.
"""

from __future__ import annotations

import psycopg
import pytest

from apps.common.tests.factories import AssetFactory, TenantFactory
from apps.stock.models import StockItem, StockTxn
from conftest import set_app_role_tenant


@pytest.mark.django_db(transaction=True)
def test_raw_update_of_a_stock_txn_is_rejected_at_the_db(app_role_connection):
    tenant = TenantFactory()
    asset = AssetFactory(tenant=tenant, is_consumable=True)
    stock_item = StockItem.all_objects.create(
        tenant=tenant, asset=asset, unit_of_measure="unit", reorder_threshold=5
    )
    txn = StockTxn.all_objects.create(
        tenant=tenant, stock_item=stock_item, delta=10, reason=StockTxn.Reason.RECEIVE
    )

    set_app_role_tenant(app_role_connection, tenant.id)
    with app_role_connection.cursor() as cur:
        with pytest.raises(psycopg.errors.RestrictViolation) as exc_info:
            cur.execute("UPDATE stock_stock_txn SET delta = %s WHERE id = %s", [999, txn.id])
        assert "append-only" in str(exc_info.value)
    # The raised exception aborts the current transaction on this
    # connection; roll back before issuing any further statement on it.
    app_role_connection.rollback()

    # The row in the DB is untouched — verified via a FRESH statement on the
    # same connection (the aborted UPDATE above never committed).
    with app_role_connection.cursor() as cur:
        set_app_role_tenant(app_role_connection, tenant.id)
        cur.execute("SELECT delta FROM stock_stock_txn WHERE id = %s", [txn.id])
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 10


@pytest.mark.django_db(transaction=True)
def test_raw_delete_of_a_stock_txn_is_rejected_at_the_db(app_role_connection):
    tenant = TenantFactory()
    asset = AssetFactory(tenant=tenant, is_consumable=True)
    stock_item = StockItem.all_objects.create(
        tenant=tenant, asset=asset, unit_of_measure="unit", reorder_threshold=5
    )
    txn = StockTxn.all_objects.create(
        tenant=tenant, stock_item=stock_item, delta=10, reason=StockTxn.Reason.RECEIVE
    )

    set_app_role_tenant(app_role_connection, tenant.id)
    with app_role_connection.cursor() as cur:
        with pytest.raises(psycopg.errors.RestrictViolation) as exc_info:
            cur.execute("DELETE FROM stock_stock_txn WHERE id = %s", [txn.id])
        assert "append-only" in str(exc_info.value)
    app_role_connection.rollback()

    with app_role_connection.cursor() as cur:
        set_app_role_tenant(app_role_connection, tenant.id)
        cur.execute("SELECT id FROM stock_stock_txn WHERE id = %s", [txn.id])
        assert cur.fetchone() is not None, "The row must still exist — the DELETE was rejected."


@pytest.mark.django_db(transaction=True)
def test_a_correction_is_a_new_row_visible_alongside_the_original_at_the_db(app_role_connection):
    """The other half of "immutable new row, not an edit": a `correction`
    txn is a plain second INSERT (never blocked), and both rows remain
    independently visible/queryable via the raw RLS-subject connection —
    proving the append-only trigger blocks mutation specifically, not all
    writes to the table.
    """
    tenant = TenantFactory()
    asset = AssetFactory(tenant=tenant, is_consumable=True)
    stock_item = StockItem.all_objects.create(
        tenant=tenant, asset=asset, unit_of_measure="unit", reorder_threshold=5
    )
    original = StockTxn.all_objects.create(
        tenant=tenant, stock_item=stock_item, delta=10, reason=StockTxn.Reason.RECEIVE
    )
    correction = StockTxn.all_objects.create(
        tenant=tenant,
        stock_item=stock_item,
        delta=-2,
        reason=StockTxn.Reason.CORRECTION,
        ref="miscount",
    )

    set_app_role_tenant(app_role_connection, tenant.id)
    with app_role_connection.cursor() as cur:
        cur.execute(
            "SELECT id, delta, reason FROM stock_stock_txn " "WHERE stock_item_id = %s ORDER BY id",
            [stock_item.id],
        )
        rows = cur.fetchall()
        assert [r[0] for r in rows] == [original.id, correction.id]
        assert rows[0][1:] == (10, "receive")
        assert rows[1][1:] == (-2, "correction")
