"""Counted assets (`tracking`/`quantity`) through `/api/v1/assets`: validation
mirrors the DB constraints as 400s (incl. PATCH fallbacks and the
individual->counted switch guard), the `?tracking=` filter, audit capture, and
tenant isolation of the filter.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.assets.models import Asset
from apps.audit.models import AuditLog
from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
    AssetFactory,
    CategoryFactory,
    TenantFactory,
    UserFactory,
    upgrade_tenant_wide_role,
)
from apps.rbac.permission_keys import ROLE_ADMIN
from apps.reservations.models import Checkout, Reservation

pytestmark = pytest.mark.django_db


def _setup(client):
    tenant = TenantFactory()
    admin = UserFactory(tenant=tenant)
    upgrade_tenant_wide_role(admin, ROLE_ADMIN)
    response = client.post(
        "/api/v1/auth/login",
        {"tenant": tenant.slug, "email": admin.email, "password": DEFAULT_TEST_PASSWORD},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return tenant, admin


def _post(client, **body):
    return client.post("/api/v1/assets/", data=json.dumps(body), content_type="application/json")


def _patch(client, asset, **body):
    return client.patch(
        f"/api/v1/assets/{asset.id}/", data=json.dumps(body), content_type="application/json"
    )


@pytest.fixture
def ctx(client):
    tenant, admin = _setup(client)
    durable = CategoryFactory(tenant=tenant, name="Tools", default_is_consumable=False)
    consumable = CategoryFactory(tenant=tenant, name="Screws", default_is_consumable=True)
    return tenant, admin, durable, consumable


class TestCreateValidation:
    def test_default_is_individual_quantity_one(self, client, ctx):
        tenant, _, durable, _ = ctx
        r = _post(client, category=durable.id, name="Drone")
        assert r.status_code == 201, r.content
        assert (r.json()["tracking"], r.json()["quantity"]) == ("individual", 1)

    def test_counted_with_quantity(self, client, ctx):
        _, _, durable, _ = ctx
        r = _post(client, category=durable.id, name="Screwdrivers", tracking="counted", quantity=12)
        assert r.status_code == 201, r.content
        assert (r.json()["tracking"], r.json()["quantity"]) == ("counted", 12)

    def test_quantity_without_tracking_on_create_is_rejected(self, client, ctx):
        _, _, durable, _ = ctx
        r = _post(client, category=durable.id, name="X", quantity=5)
        assert r.status_code == 400
        assert "quantity" in r.json()["errors"]

    def test_individual_with_quantity_gt_one_is_400(self, client, ctx):
        _, _, durable, _ = ctx
        r = _post(client, category=durable.id, name="X", tracking="individual", quantity=2)
        assert r.status_code == 400

    @pytest.mark.parametrize("qty", [0, -1])
    def test_non_positive_quantity_is_400_not_500(self, client, ctx, qty):
        _, _, durable, _ = ctx
        r = _post(client, category=durable.id, name="X", tracking="counted", quantity=qty)
        assert r.status_code == 400

    def test_unknown_tracking_is_400(self, client, ctx):
        _, _, durable, _ = ctx
        assert _post(client, category=durable.id, name="X", tracking="bogus").status_code == 400

    def test_counted_in_consumable_category_is_400_after_default(self, client, ctx):
        _, _, _, consumable = ctx
        r = _post(client, category=consumable.id, name="Screws", tracking="counted", quantity=3)
        assert r.status_code == 400
        assert "tracking" in json.dumps(r.json())

    def test_counted_with_explicit_consumable_is_400(self, client, ctx):
        _, _, durable, _ = ctx
        r = _post(
            client,
            category=durable.id,
            name="X",
            tracking="counted",
            quantity=3,
            is_consumable=True,
        )
        assert r.status_code == 400


class TestPatchValidation:
    def test_individual_to_counted_with_quantity(self, client, ctx):
        tenant, _, durable, _ = ctx
        asset = AssetFactory(tenant=tenant, category=durable)
        r = _patch(client, asset, tracking="counted", quantity=4)
        assert r.status_code == 200, r.content
        assert (r.json()["tracking"], r.json()["quantity"]) == ("counted", 4)

    def test_individual_to_counted_without_quantity_keeps_one(self, client, ctx):
        tenant, _, durable, _ = ctx
        asset = AssetFactory(tenant=tenant, category=durable)
        r = _patch(client, asset, tracking="counted")
        assert r.status_code == 200, r.content
        assert r.json()["quantity"] == 1

    def test_patch_quantity_on_individual_is_400(self, client, ctx):
        tenant, _, durable, _ = ctx
        asset = AssetFactory(tenant=tenant, category=durable)
        assert _patch(client, asset, quantity=3).status_code == 400

    def test_patch_quantity_only_on_counted_falls_back_to_instance_tracking(self, client, ctx):
        tenant, _, durable, _ = ctx
        asset = AssetFactory(
            tenant=tenant, category=durable, tracking=Asset.Tracking.COUNTED, quantity=5
        )
        r = _patch(client, asset, quantity=9)
        assert r.status_code == 200, r.content
        assert r.json()["quantity"] == 9

    def test_counted_patch_resending_tracking_counted_with_new_quantity_succeeds(self, client, ctx):
        # The edit form always sends tracking+quantity; the switch guard must
        # only fire on a real individual->counted transition.
        tenant, admin, durable, _ = ctx
        asset = AssetFactory(
            tenant=tenant, category=durable, tracking=Asset.Tracking.COUNTED, quantity=5
        )
        # Even a stray open checkout / active reservation (legacy data) must
        # not block a counted->counted edit.
        Checkout.all_objects.create(
            tenant=tenant,
            asset=asset,
            user=admin,
            checked_out_at=timezone.now(),
            due_at=timezone.now() + timedelta(days=1),
        )
        r = _patch(client, asset, tracking="counted", quantity=8)
        assert r.status_code == 200, r.content
        assert r.json()["quantity"] == 8

    def test_individual_patch_resending_individual_and_one_succeeds(self, client, ctx):
        tenant, _, durable, _ = ctx
        asset = AssetFactory(tenant=tenant, category=durable)
        r = _patch(client, asset, tracking="individual", quantity=1, name="Same")
        assert r.status_code == 200, r.content

    def test_validation_errors_use_field_keyed_problem_json(self, client, ctx):
        tenant, _, durable, _ = ctx
        asset = AssetFactory(tenant=tenant, category=durable)
        r = _patch(client, asset, tracking="individual", quantity=3)
        assert r.status_code == 400
        assert isinstance(r.json()["errors"]["quantity"], list)

    def test_counted_to_individual_requires_quantity_one(self, client, ctx):
        tenant, _, durable, _ = ctx
        asset = AssetFactory(
            tenant=tenant, category=durable, tracking=Asset.Tracking.COUNTED, quantity=5
        )
        r = _patch(client, asset, tracking="individual")
        assert r.status_code == 400
        assert "quantity to 1" in json.dumps(r.json())
        ok = _patch(client, asset, tracking="individual", quantity=1)
        assert ok.status_code == 200, ok.content

    def test_flagging_counted_asset_consumable_is_400(self, client, ctx):
        tenant, _, durable, _ = ctx
        asset = AssetFactory(
            tenant=tenant, category=durable, tracking=Asset.Tracking.COUNTED, quantity=5
        )
        assert _patch(client, asset, is_consumable=True).status_code == 400

    def test_counting_a_consumable_asset_is_400(self, client, ctx):
        tenant, _, _, consumable = ctx
        asset = AssetFactory(tenant=tenant, category=consumable, is_consumable=True)
        assert _patch(client, asset, tracking="counted", quantity=2).status_code == 400

    def test_unrelated_patch_leaves_counted_state_alone(self, client, ctx):
        tenant, _, durable, _ = ctx
        asset = AssetFactory(
            tenant=tenant, category=durable, tracking=Asset.Tracking.COUNTED, quantity=5
        )
        r = _patch(client, asset, name="Renamed")
        assert r.status_code == 200
        assert (r.json()["tracking"], r.json()["quantity"]) == ("counted", 5)


class TestSwitchGuards:
    def _reservation(self, tenant, asset, user, status):
        start = timezone.now() + timedelta(hours=1)
        return Reservation.all_objects.create(
            tenant=tenant,
            asset=asset,
            user=user,
            start_at=start,
            end_at=start + timedelta(hours=1),
            status=status,
        )

    @pytest.mark.parametrize("status", Reservation.ACTIVE_STATUSES)
    def test_active_reservation_blocks_switch(self, client, ctx, status):
        tenant, admin, durable, _ = ctx
        asset = AssetFactory(tenant=tenant, category=durable)
        self._reservation(tenant, asset, admin, status)
        r = _patch(client, asset, tracking="counted", quantity=2)
        assert r.status_code == 400
        assert "tracking" in json.dumps(r.json())
        asset.refresh_from_db()
        assert asset.tracking == "individual"

    def test_terminal_reservation_does_not_block(self, client, ctx):
        tenant, admin, durable, _ = ctx
        asset = AssetFactory(tenant=tenant, category=durable)
        self._reservation(tenant, asset, admin, Reservation.Status.CANCELLED)
        assert _patch(client, asset, tracking="counted", quantity=2).status_code == 200

    def test_stock_item_blocks_switch(self, client, ctx):
        from apps.common.tests.factories import StockItemFactory

        tenant, _, _, consumable = ctx
        item = StockItemFactory(tenant=tenant)
        # Un-consumable the asset, leaving its StockItem behind.
        Asset.all_objects.filter(pk=item.asset_id).update(is_consumable=False)
        r = _patch(client, item.asset, tracking="counted", quantity=2)
        assert r.status_code == 400
        assert "stock" in json.dumps(r.json()["errors"]["tracking"]).lower()

    def test_open_checkout_blocks_switch(self, client, ctx):
        tenant, admin, durable, _ = ctx
        asset = AssetFactory(tenant=tenant, category=durable)
        Checkout.all_objects.create(
            tenant=tenant,
            asset=asset,
            user=admin,
            checked_out_at=timezone.now(),
            due_at=timezone.now() + timedelta(days=1),
        )
        r = _patch(client, asset, tracking="counted", quantity=2)
        assert r.status_code == 400
        assert "checked out" in json.dumps(r.json())

    def test_closed_checkout_does_not_block(self, client, ctx):
        tenant, admin, durable, _ = ctx
        asset = AssetFactory(tenant=tenant, category=durable)
        Checkout.all_objects.create(
            tenant=tenant,
            asset=asset,
            user=admin,
            due_at=timezone.now() + timedelta(days=1),
            checked_out_at=timezone.now(),
            checked_in_at=timezone.now(),
        )
        assert _patch(client, asset, tracking="counted", quantity=2).status_code == 200


class TestFilterAndAudit:
    def test_tracking_filter(self, client, ctx):
        tenant, _, durable, _ = ctx
        AssetFactory(tenant=tenant, category=durable, name="Solo")
        AssetFactory(tenant=tenant, category=durable, name="Box", tracking="counted", quantity=10)
        counted = client.get("/api/v1/assets/?tracking=counted").json()["results"]
        assert [a["name"] for a in counted] == ["Box"]
        individual = client.get("/api/v1/assets/?tracking=individual").json()["results"]
        assert [a["name"] for a in individual] == ["Solo"]
        assert client.get("/api/v1/assets/?tracking=bogus").status_code == 400

    def test_filter_never_leaks_other_tenant(self, client, ctx):
        tenant, _, durable, _ = ctx
        other = TenantFactory()
        AssetFactory(
            tenant=other,
            category=CategoryFactory(tenant=other),
            name="Foreign box",
            tracking="counted",
            quantity=7,
        )
        AssetFactory(tenant=tenant, category=durable, name="Mine", tracking="counted", quantity=2)
        names = [
            a["name"] for a in client.get("/api/v1/assets/?tracking=counted").json()["results"]
        ]
        assert names == ["Mine"]

    def test_create_and_switch_are_audited_with_tracking_and_quantity(self, client, ctx):
        tenant, _, durable, _ = ctx
        created = _post(client, category=durable.id, name="Bits", tracking="counted", quantity=30)
        asset_id = created.json()["id"]
        entry = AuditLog.all_objects.get(
            tenant_id=tenant.id, entity_type="asset", entity_id=str(asset_id), action="asset.create"
        )
        assert entry.after is not None
        assert entry.after["tracking"] == "counted" and entry.after["quantity"] == 30

        asset = Asset.all_objects.get(pk=asset_id)
        assert _patch(client, asset, quantity=25).status_code == 200
        edit = AuditLog.all_objects.get(
            tenant_id=tenant.id, entity_type="asset", entity_id=str(asset_id), action="asset.edit"
        )
        assert edit.before is not None and edit.after is not None
        assert edit.before["quantity"] == 30 and edit.after["quantity"] == 25
        assert edit.before["tracking"] == edit.after["tracking"] == "counted"

    def test_unrelated_edit_writes_no_audit(self, client, ctx):
        tenant, _, durable, _ = ctx
        asset = AssetFactory(tenant=tenant, category=durable)
        assert _patch(client, asset, name="New").status_code == 200
        assert not AuditLog.all_objects.filter(
            tenant_id=tenant.id, entity_type="asset", action="asset.edit"
        ).exists()


class TestEditRaceSafety:
    def test_edit_rechecks_guard_after_lock(self, client, ctx, monkeypatch):
        """A reservation that appears between validate() and the row lock must
        still block the switch (guard re-asserted on the locked row)."""
        from apps.assets.serializers import AssetSerializer

        tenant, admin, durable, _ = ctx
        asset = AssetFactory(tenant=tenant, category=durable)
        original = AssetSerializer._assert_can_become_counted
        calls = {"n": 0}

        def sneaky(a):
            calls["n"] += 1
            if calls["n"] == 1:  # validate()-time check passes (nothing yet)...
                original(a)
                start = timezone.now() + timedelta(hours=1)
                Reservation.all_objects.create(
                    tenant=tenant,
                    asset=asset,
                    user=admin,
                    start_at=start,
                    end_at=start + timedelta(hours=1),
                    status=Reservation.Status.APPROVED,
                )
                return
            original(a)  # ...the under-lock re-check must now fire

        monkeypatch.setattr(AssetSerializer, "_assert_can_become_counted", staticmethod(sneaky))
        r = _patch(client, asset, tracking="counted", quantity=2)
        assert r.status_code == 400, r.content
        asset.refresh_from_db()
        assert asset.tracking == "individual"

    def test_edit_does_not_overwrite_concurrent_status_change(self, client, ctx, monkeypatch):
        """The edit is applied on the locked row, so a status flip made after
        the view loaded its instance (e.g. a check-in) survives the PATCH."""
        from apps.assets.serializers import AssetSerializer

        tenant, _, durable, _ = ctx
        asset = AssetFactory(tenant=tenant, category=durable, status=Asset.Status.IN_USE)
        original = AssetSerializer.validate

        def validate_then_flip(self, attrs):
            out = original(self, attrs)
            Asset.all_objects.filter(pk=asset.pk).update(status=Asset.Status.AVAILABLE)
            return out

        monkeypatch.setattr(AssetSerializer, "validate", validate_then_flip)
        r = _patch(client, asset, name="Renamed")
        assert r.status_code == 200, r.content
        assert r.json()["status"] == "available"
        asset.refresh_from_db()
        assert (asset.name, asset.status) == ("Renamed", "available")

    def test_edit_audit_before_comes_from_locked_row(self, client, ctx, monkeypatch):
        from apps.assets.serializers import AssetSerializer

        tenant, _, durable, _ = ctx
        asset = AssetFactory(
            tenant=tenant, category=durable, tracking=Asset.Tracking.COUNTED, quantity=5
        )
        original = AssetSerializer.validate

        def validate_then_bump(self, attrs):
            out = original(self, attrs)
            Asset.all_objects.filter(pk=asset.pk).update(quantity=6)
            return out

        monkeypatch.setattr(AssetSerializer, "validate", validate_then_bump)
        assert _patch(client, asset, quantity=9).status_code == 200
        edit = AuditLog.all_objects.get(
            tenant_id=tenant.id, entity_type="asset", action="asset.edit"
        )
        assert edit.before is not None and edit.after is not None
        assert edit.before["quantity"] == 6 and edit.after["quantity"] == 9

    def test_concurrent_change_yields_400_not_integrity_error(self, client, ctx, monkeypatch):
        """validate() sees counted/qty 1 -> individual is fine; a concurrent
        edit then makes quantity 5. The under-lock re-validation must 400."""
        from apps.assets.serializers import AssetSerializer

        tenant, _, durable, _ = ctx
        asset = AssetFactory(
            tenant=tenant, category=durable, tracking=Asset.Tracking.COUNTED, quantity=1
        )
        original = AssetSerializer.validate

        def validate_then_bump(self, attrs):
            out = original(self, attrs)
            Asset.all_objects.filter(pk=asset.pk).update(quantity=5)
            return out

        monkeypatch.setattr(AssetSerializer, "validate", validate_then_bump)
        r = _patch(client, asset, tracking="individual")
        assert r.status_code == 400, r.content
        # (the simulated concurrent write shares the request's rolled-back
        # transaction, so only the refusal itself is asserted)
        asset.refresh_from_db()
        assert asset.tracking == "counted"
