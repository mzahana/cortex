"""`Asset.tracking` / `Asset.quantity` — counted-asset invariants enforced at
the DB level (migration `assets.0006_asset_tracking_quantity`).

Each `CHECK` is proven two ways: the ORM write path is rejected with
`IntegrityError`, and a raw SQL `UPDATE` (bypassing every app-level guard,
serializer, and model default) is rejected too — the DB, not the app, is the
backstop. Valid combinations are proven to be accepted.
"""

from __future__ import annotations

import io

import pytest
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction

from apps.assets.models import Asset
from apps.common.tests.factories import AssetFactory

pytestmark = pytest.mark.django_db


def _raw_update(asset_id: int, **cols) -> None:
    sets = ", ".join(f"{col} = %s" for col in cols)
    with connection.cursor() as cur:
        cur.execute(f"UPDATE assets_asset SET {sets} WHERE id = %s", [*cols.values(), asset_id])


def _assert_rejected(constraint: str, fn) -> None:
    with pytest.raises(IntegrityError) as exc:
        with transaction.atomic():
            fn()
    assert constraint in str(exc.value)


class TestDefaults:
    def test_new_asset_defaults_to_individual_quantity_one(self):
        asset = AssetFactory()
        asset.refresh_from_db()
        assert asset.tracking == Asset.Tracking.INDIVIDUAL
        assert asset.quantity == 1


class TestValidRowsAccepted:
    def test_counted_durable_with_quantity_many(self):
        asset = AssetFactory(tracking=Asset.Tracking.COUNTED, quantity=25)
        asset.refresh_from_db()
        assert (asset.tracking, asset.quantity) == ("counted", 25)

    def test_counted_durable_with_quantity_one(self):
        asset = AssetFactory(tracking=Asset.Tracking.COUNTED, quantity=1)
        asset.refresh_from_db()
        assert asset.quantity == 1

    def test_individual_consumable_quantity_one(self):
        asset = AssetFactory(is_consumable=True)
        asset.refresh_from_db()
        assert (asset.tracking, asset.quantity) == ("individual", 1)

    def test_counted_to_individual_round_trip_via_sql(self):
        asset = AssetFactory(tracking=Asset.Tracking.COUNTED, quantity=3)
        _raw_update(asset.pk, tracking="individual", quantity=1)
        asset.refresh_from_db()
        assert (asset.tracking, asset.quantity) == ("individual", 1)


class TestTrackingValid:
    def test_orm_rejects_unknown_tracking(self):
        _assert_rejected("asset_tracking_valid", lambda: AssetFactory(tracking="bogus"))

    def test_sql_rejects_unknown_tracking(self):
        asset = AssetFactory()
        _assert_rejected("asset_tracking_valid", lambda: _raw_update(asset.pk, tracking="bogus"))


class TestQuantityPositive:
    def test_orm_rejects_zero_quantity_on_counted(self):
        _assert_rejected(
            "asset_quantity_positive",
            lambda: AssetFactory(tracking=Asset.Tracking.COUNTED, quantity=0),
        )

    def test_sql_rejects_zero_quantity_on_counted(self):
        asset = AssetFactory(tracking=Asset.Tracking.COUNTED, quantity=5)
        _assert_rejected("asset_quantity_positive", lambda: _raw_update(asset.pk, quantity=0))


class TestIndividualQuantityOne:
    def test_orm_rejects_individual_with_quantity_many(self):
        _assert_rejected(
            "asset_individual_quantity_one",
            lambda: AssetFactory(tracking=Asset.Tracking.INDIVIDUAL, quantity=2),
        )

    def test_sql_rejects_individual_with_quantity_many(self):
        asset = AssetFactory()
        _assert_rejected("asset_individual_quantity_one", lambda: _raw_update(asset.pk, quantity=7))

    def test_sql_rejects_flipping_counted_to_individual_without_resetting_quantity(self):
        asset = AssetFactory(tracking=Asset.Tracking.COUNTED, quantity=4)
        _assert_rejected(
            "asset_individual_quantity_one",
            lambda: _raw_update(asset.pk, tracking="individual"),
        )


class TestConsumableNotCounted:
    def test_orm_rejects_counted_consumable(self):
        _assert_rejected(
            "asset_consumable_not_counted",
            lambda: AssetFactory(is_consumable=True, tracking=Asset.Tracking.COUNTED, quantity=3),
        )

    def test_sql_rejects_marking_counted_asset_consumable(self):
        asset = AssetFactory(tracking=Asset.Tracking.COUNTED, quantity=3)
        _assert_rejected(
            "asset_consumable_not_counted", lambda: _raw_update(asset.pk, is_consumable=True)
        )


def test_makemigrations_check_is_clean():
    out = io.StringIO()
    # Raises SystemExit(1) if any model change lacks a migration.
    call_command("makemigrations", "--check", "--dry-run", stdout=out, stderr=out)
    assert "No changes detected" in out.getvalue()
