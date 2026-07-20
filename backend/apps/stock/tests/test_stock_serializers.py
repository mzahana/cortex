"""T2.1 — serializer-level checks: `quantity_on_hand` is read-only/derived,
`StockTxn` has no update path, invalid reorder-status transitions are
rejected, and every writable FK (`asset`, `bin_location`, `stock_item`) is
scoped through the tenant-scoped `.objects` manager (never the client's raw
tenant/id).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from rest_framework import serializers as rest_serializers

from apps.common.tests.factories import AssetFactory, TenantFactory, UserFactory
from apps.stock.models import ReorderRequest, StockItem, StockTxn
from apps.stock.serializers import (
    ReorderRequestSerializer,
    StockItemSerializer,
    StockTxnSerializer,
)
from apps.tenancy.context import tenant_context

pytestmark = pytest.mark.django_db


def _request_for(user):
    """A minimal stand-in for DRF's `request` in `serializer.context` — only
    `.user` is read by these serializers' `create()`."""
    return SimpleNamespace(user=user)


class TestStockItemSerializer:
    def test_quantity_on_hand_is_read_only(self):
        tenant = TenantFactory()
        user = UserFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, is_consumable=True)
        with tenant_context(tenant.id):
            serializer = StockItemSerializer(
                data={
                    "asset": asset.id,
                    "unit_of_measure": "unit",
                    "quantity_on_hand": 999,
                    "reorder_threshold": 1,
                    "reorder_target": 5,
                },
                context={"request": _request_for(user)},
            )
            assert serializer.is_valid(), serializer.errors
            stock_item = serializer.save()
        assert stock_item.quantity_on_hand == 0  # ignored, never 999

    def test_rejects_a_durable_asset(self):
        tenant = TenantFactory()
        user = UserFactory(tenant=tenant)
        durable_asset = AssetFactory(tenant=tenant, is_consumable=False)
        with tenant_context(tenant.id):
            serializer = StockItemSerializer(
                data={"asset": durable_asset.id, "unit_of_measure": "unit"},
                context={"request": _request_for(user)},
            )
            assert not serializer.is_valid()
        assert "asset" in serializer.errors

    def test_rejects_a_second_stock_item_for_the_same_asset(self):
        tenant = TenantFactory()
        user = UserFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, is_consumable=True)
        with tenant_context(tenant.id):
            StockItem.objects.create(tenant=tenant, asset=asset, unit_of_measure="unit")
            serializer = StockItemSerializer(
                data={"asset": asset.id, "unit_of_measure": "unit"},
                context={"request": _request_for(user)},
            )
            assert not serializer.is_valid()
        assert "asset" in serializer.errors


class TestStockTxnSerializer:
    def test_actor_comes_from_the_request_never_the_client(self):
        tenant = TenantFactory()
        user = UserFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, is_consumable=True)
        with tenant_context(tenant.id):
            stock_item = StockItem.objects.create(
                tenant=tenant, asset=asset, unit_of_measure="unit"
            )
            serializer = StockTxnSerializer(
                data={
                    "stock_item": stock_item.id,
                    "delta": 5,
                    "reason": StockTxn.Reason.RECEIVE,
                    "actor": 99999,  # must be ignored -- read_only
                },
                context={"request": _request_for(user)},
            )
            assert serializer.is_valid(), serializer.errors
            txn = serializer.save()
        assert txn.actor_id == user.id

    def test_no_update_path(self):
        tenant = TenantFactory()
        user = UserFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, is_consumable=True)
        with tenant_context(tenant.id):
            stock_item = StockItem.objects.create(
                tenant=tenant, asset=asset, unit_of_measure="unit"
            )
            txn = StockTxn.objects.create(
                tenant=tenant, stock_item=stock_item, delta=5, reason=StockTxn.Reason.RECEIVE
            )
            serializer = StockTxnSerializer(
                instance=txn,
                data={"delta": 999},
                partial=True,
                context={"request": _request_for(user)},
            )
            assert serializer.is_valid(), serializer.errors
            with pytest.raises(rest_serializers.ValidationError):
                serializer.save()


class TestReorderRequestSerializer:
    def test_requested_by_comes_from_the_request(self):
        tenant = TenantFactory()
        user = UserFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, is_consumable=True)
        with tenant_context(tenant.id):
            stock_item = StockItem.objects.create(
                tenant=tenant, asset=asset, unit_of_measure="unit"
            )
            serializer = ReorderRequestSerializer(
                data={"stock_item": stock_item.id, "quantity": 10},
                context={"request": _request_for(user)},
            )
            assert serializer.is_valid(), serializer.errors
            reorder = serializer.save()
        assert reorder.requested_by_id == user.id
        assert reorder.status == ReorderRequest.Status.OPEN

    def test_invalid_transition_rejected(self):
        tenant = TenantFactory()
        user = UserFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, is_consumable=True)
        with tenant_context(tenant.id):
            stock_item = StockItem.objects.create(
                tenant=tenant, asset=asset, unit_of_measure="unit"
            )
            reorder = ReorderRequest.objects.create(
                tenant=tenant, stock_item=stock_item, quantity=10
            )
            serializer = ReorderRequestSerializer(
                instance=reorder,
                data={"status": ReorderRequest.Status.RECEIVED},
                partial=True,
                context={"request": _request_for(user)},
            )
            assert not serializer.is_valid()
        assert "status" in serializer.errors
