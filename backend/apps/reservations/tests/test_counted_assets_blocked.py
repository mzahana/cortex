"""Counted assets are never reservable or checkout-able (400), incl. the
under-lock re-check that closes the race with a concurrent tracking switch."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework import serializers

from apps.assets.models import Asset
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
from apps.reservations.services import create_reservation
from apps.tenancy.context import tenant_context

pytestmark = pytest.mark.django_db


def _login(client, tenant, user):
    r = client.post(
        "/api/v1/auth/login",
        {"tenant": tenant.slug, "email": user.email, "password": DEFAULT_TEST_PASSWORD},
        content_type="application/json",
    )
    assert r.status_code == 200, r.content


def _counted(tenant):
    return AssetFactory(
        tenant=tenant,
        category=CategoryFactory(tenant=tenant),
        tracking=Asset.Tracking.COUNTED,
        quantity=10,
    )


def test_reservation_on_counted_asset_is_400(client):
    tenant = TenantFactory()
    user = UserFactory(tenant=tenant)
    asset = _counted(tenant)
    _login(client, tenant, user)
    start = timezone.now() + timedelta(hours=1)
    r = client.post(
        "/api/v1/reservations/",
        data=json.dumps(
            {
                "asset": asset.id,
                "start_at": start.isoformat(),
                "end_at": (start + timedelta(hours=2)).isoformat(),
            }
        ),
        content_type="application/json",
    )
    assert r.status_code == 400, r.content
    assert "Counted assets can't be reserved" in json.dumps(r.json())
    with tenant_context(tenant.id):
        assert not Reservation.objects.exists()


def test_checkout_on_counted_asset_is_400(client):
    tenant = TenantFactory()
    user = UserFactory(tenant=tenant)
    asset = _counted(tenant)
    _login(client, tenant, user)
    r = client.post(
        "/api/v1/checkouts/",
        data=json.dumps(
            {"asset": asset.id, "due_at": (timezone.now() + timedelta(days=1)).isoformat()}
        ),
        content_type="application/json",
    )
    assert r.status_code == 400, r.content
    assert "Counted assets can't be checked out" in json.dumps(r.json())
    with tenant_context(tenant.id):
        assert not Checkout.objects.exists()


def test_service_rechecks_under_lock_when_asset_became_counted():
    """Stale (individual) asset object passed in, row is counted by now."""
    tenant = TenantFactory()
    user = UserFactory(tenant=tenant)
    asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
    Asset.all_objects.filter(pk=asset.pk).update(tracking="counted", quantity=3)
    start = timezone.now() + timedelta(hours=1)
    with tenant_context(tenant.id):
        with pytest.raises(serializers.ValidationError):
            create_reservation(
                tenant=tenant,
                actor=user,
                asset=asset,  # stale in-memory copy: still 'individual'
                start_at=start,
                end_at=start + timedelta(hours=1),
            )
        assert not Reservation.objects.exists()


def test_individual_asset_still_reservable_and_checkoutable(client):
    tenant = TenantFactory()
    user = UserFactory(tenant=tenant)
    upgrade_tenant_wide_role(user, ROLE_ADMIN)
    asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
    _login(client, tenant, user)
    r = client.post(
        "/api/v1/checkouts/",
        data=json.dumps(
            {"asset": asset.id, "due_at": (timezone.now() + timedelta(days=1)).isoformat()}
        ),
        content_type="application/json",
    )
    assert r.status_code == 201, r.content


def test_checkout_create_rechecks_under_lock_when_asset_became_counted():
    """Serializer validate() saw an individual asset; the row is counted by
    the time `create()` takes the lock."""
    from unittest.mock import MagicMock

    from apps.reservations.checkout import CheckoutSerializer

    tenant = TenantFactory()
    user = UserFactory(tenant=tenant)
    asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
    Asset.all_objects.filter(pk=asset.pk).update(tracking="counted", quantity=3)
    request = MagicMock()
    request.user = user
    serializer = CheckoutSerializer(context={"request": request})
    with tenant_context(tenant.id):
        with pytest.raises(serializers.ValidationError):
            serializer.create(
                {"asset": asset, "due_at": timezone.now() + timedelta(days=1)}  # stale asset
            )
        assert not Checkout.objects.exists()
