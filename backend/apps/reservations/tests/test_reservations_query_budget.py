"""T3.6 — CLAUDE.md query-budget discipline: `GET /reservations` and
`GET /checkouts` must not N+1 as the row count grows (each row's serializer
touches `asset`/`asset__project`/`user`/`project`/`approver` /
`reservation`, all `select_related`-able in one query per the api.py/
checkout.py `get_queryset()` implementations)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

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
from apps.tenancy.context import tenant_context

pytestmark = pytest.mark.django_db


def _login(client, tenant, user):
    response = client.post(
        "/api/v1/auth/login",
        {"tenant": tenant.slug, "email": user.email, "password": DEFAULT_TEST_PASSWORD},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return response


class TestQueryBudget:
    def test_reservations_list_query_count_does_not_grow_with_row_count(
        self, client, django_assert_max_num_queries
    ):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = CategoryFactory(tenant=tenant)

        with tenant_context(tenant.id):
            for i in range(20):
                asset = AssetFactory(tenant=tenant, category=category)
                Reservation.objects.create(
                    tenant=tenant,
                    asset=asset,
                    user=admin,
                    start_at=timezone.now() + timedelta(hours=i + 1),
                    end_at=timezone.now() + timedelta(hours=i + 2),
                    status=Reservation.Status.APPROVED,
                )

        _login(client, tenant, admin)
        with django_assert_max_num_queries(20) as ctx_small:
            response = client.get("/api/v1/reservations/")
        assert response.status_code == 200, response.content
        assert response.json()["count"] == 20
        small_queries = len(ctx_small.captured_queries)

        with tenant_context(tenant.id):
            for i in range(20, 60):
                asset = AssetFactory(tenant=tenant, category=category)
                Reservation.objects.create(
                    tenant=tenant,
                    asset=asset,
                    user=admin,
                    start_at=timezone.now() + timedelta(hours=i + 1),
                    end_at=timezone.now() + timedelta(hours=i + 2),
                    status=Reservation.Status.APPROVED,
                )

        with django_assert_max_num_queries(20) as ctx_large:
            response = client.get("/api/v1/reservations/")
        assert response.status_code == 200, response.content
        assert response.json()["count"] == 60
        # The query count must not scale with the row count (no N+1) —
        # constant regardless of how many more reservations exist.
        assert len(ctx_large.captured_queries) == small_queries

    def test_checkouts_list_query_count_does_not_grow_with_row_count(
        self, client, django_assert_max_num_queries
    ):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = CategoryFactory(tenant=tenant)

        with tenant_context(tenant.id):
            for _i in range(20):
                asset = AssetFactory(tenant=tenant, category=category)
                Checkout.objects.create(
                    tenant=tenant,
                    asset=asset,
                    user=admin,
                    checked_out_at=timezone.now(),
                    due_at=timezone.now() + timedelta(days=1),
                )

        _login(client, tenant, admin)
        with django_assert_max_num_queries(20) as ctx_small:
            response = client.get("/api/v1/checkouts/")
        assert response.status_code == 200, response.content
        assert response.json()["count"] == 20
        small_queries = len(ctx_small.captured_queries)

        with tenant_context(tenant.id):
            for _i in range(20, 60):
                asset = AssetFactory(tenant=tenant, category=category)
                Checkout.objects.create(
                    tenant=tenant,
                    asset=asset,
                    user=admin,
                    checked_out_at=timezone.now(),
                    due_at=timezone.now() + timedelta(days=1),
                )

        with django_assert_max_num_queries(20) as ctx_large:
            response = client.get("/api/v1/checkouts/")
        assert response.status_code == 200, response.content
        assert response.json()["count"] == 60
        assert len(ctx_large.captured_queries) == small_queries
