"""T2.1 — StockItem / StockTxn / ReorderRequest: the consumable/durable
invariant, ledger append-only enforcement, status-transition rules, and
tenant scoping of reads.
"""

from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError

from apps.common.tests.factories import AssetFactory, TenantFactory
from apps.stock.models import ReorderRequest, StockItem, StockTxn
from apps.tenancy.context import tenant_context

pytestmark = pytest.mark.django_db


def _make_stock_item(tenant, *, is_consumable=True, **kwargs):
    asset = AssetFactory(tenant=tenant, is_consumable=is_consumable)
    defaults = dict(
        tenant=tenant,
        asset=asset,
        unit_of_measure="unit",
        reorder_threshold=5,
        reorder_target=20,
    )
    defaults.update(kwargs)
    return StockItem.all_objects.create(**defaults)


class TestConsumableInvariant:
    def test_consumable_asset_can_own_exactly_one_stock_item(self):
        tenant = TenantFactory()
        stock_item = _make_stock_item(tenant)
        assert stock_item.pk is not None
        assert stock_item.asset.is_consumable is True

        # A second StockItem for the SAME asset must be rejected — the
        # OneToOneField's implicit unique constraint enforces "at most one".
        from django.db import IntegrityError, transaction

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                StockItem.all_objects.create(
                    tenant=tenant,
                    asset=stock_item.asset,
                    unit_of_measure="unit",
                )

    def test_durable_asset_cannot_get_a_stock_item(self):
        tenant = TenantFactory()
        durable_asset = AssetFactory(tenant=tenant, is_consumable=False)
        with pytest.raises(ValidationError):
            StockItem.all_objects.create(
                tenant=tenant,
                asset=durable_asset,
                unit_of_measure="unit",
            )
        assert not StockItem.all_objects.filter(asset=durable_asset).exists()

    def test_clean_also_rejects_a_durable_asset(self):
        tenant = TenantFactory()
        durable_asset = AssetFactory(tenant=tenant, is_consumable=False)
        item = StockItem(tenant=tenant, asset=durable_asset, unit_of_measure="unit")
        with pytest.raises(ValidationError):
            item.clean()


class TestStockTxnAppendOnly:
    def test_cannot_update_an_existing_txn(self):
        tenant = TenantFactory()
        stock_item = _make_stock_item(tenant)
        txn = StockTxn.all_objects.create(
            tenant=tenant, stock_item=stock_item, delta=10, reason=StockTxn.Reason.RECEIVE
        )
        txn.delta = 999
        with pytest.raises(ValidationError):
            txn.save()

        # The row in the DB is untouched.
        txn.refresh_from_db()
        assert txn.delta == 10

    def test_cannot_delete_a_txn(self):
        tenant = TenantFactory()
        stock_item = _make_stock_item(tenant)
        txn = StockTxn.all_objects.create(
            tenant=tenant, stock_item=stock_item, delta=10, reason=StockTxn.Reason.RECEIVE
        )
        with pytest.raises(ValidationError):
            txn.delete()
        assert StockTxn.all_objects.filter(pk=txn.pk).exists()

    def test_correction_is_a_new_row_not_an_edit(self):
        tenant = TenantFactory()
        stock_item = _make_stock_item(tenant)
        StockTxn.all_objects.create(
            tenant=tenant, stock_item=stock_item, delta=10, reason=StockTxn.Reason.RECEIVE
        )
        StockTxn.all_objects.create(
            tenant=tenant,
            stock_item=stock_item,
            delta=-2,
            reason=StockTxn.Reason.CORRECTION,
            ref="miscount",
        )
        assert StockTxn.all_objects.filter(stock_item=stock_item).count() == 2
        # `recompute_quantity_on_hand()` reads through `stock_txns` (the
        # tenant-scoped reverse accessor), so it needs a tenant in context
        # like any other tenant-scoped read (T0.4 fail-closed rule).
        with tenant_context(tenant.id):
            assert stock_item.recompute_quantity_on_hand() == 8


class TestReorderRequestTransitions:
    def _make_request(self, tenant, **kwargs):
        stock_item = _make_stock_item(tenant)
        defaults = dict(tenant=tenant, stock_item=stock_item, quantity=10)
        defaults.update(kwargs)
        return ReorderRequest.all_objects.create(**defaults)

    @pytest.mark.parametrize(
        "start,target",
        [
            (ReorderRequest.Status.OPEN, ReorderRequest.Status.APPROVED),
            (ReorderRequest.Status.OPEN, ReorderRequest.Status.CANCELLED),
            (ReorderRequest.Status.APPROVED, ReorderRequest.Status.ORDERED),
            (ReorderRequest.Status.APPROVED, ReorderRequest.Status.CANCELLED),
            (ReorderRequest.Status.ORDERED, ReorderRequest.Status.RECEIVED),
            (ReorderRequest.Status.ORDERED, ReorderRequest.Status.CANCELLED),
        ],
    )
    def test_valid_transitions_allowed(self, start, target):
        tenant = TenantFactory()
        request = self._make_request(tenant, status=start)
        request.validate_transition(target)  # must not raise

    @pytest.mark.parametrize(
        "start,target",
        [
            (ReorderRequest.Status.OPEN, ReorderRequest.Status.ORDERED),
            (ReorderRequest.Status.OPEN, ReorderRequest.Status.RECEIVED),
            (ReorderRequest.Status.RECEIVED, ReorderRequest.Status.OPEN),
            (ReorderRequest.Status.CANCELLED, ReorderRequest.Status.OPEN),
            (ReorderRequest.Status.APPROVED, ReorderRequest.Status.OPEN),
        ],
    )
    def test_invalid_transitions_rejected(self, start, target):
        tenant = TenantFactory()
        request = self._make_request(tenant, status=start)
        with pytest.raises(ValidationError):
            request.validate_transition(target)

    def test_same_status_is_a_noop(self):
        tenant = TenantFactory()
        request = self._make_request(tenant, status=ReorderRequest.Status.OPEN)
        request.validate_transition(ReorderRequest.Status.OPEN)  # must not raise


class TestTenantScopedReads:
    def test_stock_item_reads_are_tenant_scoped(self):
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        item_a = _make_stock_item(tenant_a)
        _make_stock_item(tenant_b)

        with tenant_context(tenant_a.id):
            visible = list(StockItem.objects.all())
        assert visible == [item_a]

        with tenant_context(tenant_b.id):
            visible_b = list(StockItem.objects.all())
        assert item_a not in visible_b

    def test_stock_txn_reads_are_tenant_scoped(self):
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        item_a = _make_stock_item(tenant_a)
        item_b = _make_stock_item(tenant_b)
        txn_a = StockTxn.all_objects.create(
            tenant=tenant_a, stock_item=item_a, delta=5, reason=StockTxn.Reason.RECEIVE
        )
        StockTxn.all_objects.create(
            tenant=tenant_b, stock_item=item_b, delta=5, reason=StockTxn.Reason.RECEIVE
        )

        with tenant_context(tenant_a.id):
            visible = list(StockTxn.objects.all())
        assert visible == [txn_a]

    def test_reorder_request_reads_are_tenant_scoped(self):
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        item_a = _make_stock_item(tenant_a)
        item_b = _make_stock_item(tenant_b)
        req_a = ReorderRequest.all_objects.create(tenant=tenant_a, stock_item=item_a, quantity=1)
        ReorderRequest.all_objects.create(tenant=tenant_b, stock_item=item_b, quantity=1)

        with tenant_context(tenant_a.id):
            visible = list(ReorderRequest.objects.all())
        assert visible == [req_a]

    def test_no_tenant_context_fails_closed(self):
        from apps.tenancy.context import TenantContextError

        with pytest.raises(TenantContextError):
            list(StockItem.objects.all())
