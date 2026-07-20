"""T5.5 — `GET /api/v1/dashboard/summary`: tile correctness, docs/rbac.md §1
scope-aware aggregation (Admin tenant-wide vs. a pure ProjectLead scoped to
their own project only), the Redis short-TTL + event-invalidation cache
strategy, and the CLAUDE.md query-budget discipline.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
    AssetFactory,
    CategoryFactory,
    ProjectFactory,
    StockItemFactory,
    TenantFactory,
    UserFactory,
    add_project_membership,
    upgrade_tenant_wide_role,
)
from apps.dashboard.cache import invalidate_tenant_dashboard, summary_cache_key
from apps.rbac.models import Membership
from apps.rbac.permission_keys import ROLE_ADMIN, ROLE_PROJECT_LEAD
from apps.reservations.models import Checkout, Reservation
from apps.tenancy.context import tenant_context

pytestmark = pytest.mark.django_db

URL = "/api/v1/dashboard/summary"


def _login(client, tenant, user):
    response = client.post(
        "/api/v1/auth/login",
        {"tenant": tenant.slug, "email": user.email, "password": DEFAULT_TEST_PASSWORD},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return response


def _make_checkout(tenant, asset, user, *, due_in_hours=24, checked_in=False):
    with tenant_context(tenant.id):
        checkout = Checkout.objects.create(
            tenant=tenant,
            asset=asset,
            user=user,
            checked_out_at=timezone.now() - timedelta(hours=1),
            due_at=timezone.now() + timedelta(hours=due_in_hours),
        )
        if checked_in:
            checkout.checked_in_at = timezone.now()
            checkout.save(update_fields=["checked_in_at"])
    return checkout


def _make_reservation(tenant, asset, user, *, status, start_in_days=1):
    with tenant_context(tenant.id):
        return Reservation.objects.create(
            tenant=tenant,
            asset=asset,
            user=user,
            start_at=timezone.now() + timedelta(days=start_in_days),
            end_at=timezone.now() + timedelta(days=start_in_days, hours=2),
            status=status,
        )


class TestDashboardSummaryScoping:
    def test_anonymous_denied(self, client):
        response = client.get(URL)
        assert response.status_code in (401, 403)

    def test_admin_sees_tenant_wide_totals(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = CategoryFactory(tenant=tenant)
        project = ProjectFactory(tenant=tenant)

        asset_general = AssetFactory(tenant=tenant, category=category)
        asset_project = AssetFactory(tenant=tenant, category=category, project=project)
        AssetFactory(tenant=tenant, category=category, status="retired")

        _make_checkout(tenant, asset_general, admin)
        _make_checkout(tenant, asset_project, admin, due_in_hours=-1)  # overdue

        StockItemFactory(
            tenant=tenant,
            asset__tenant=tenant,
            asset__category=category,
            reorder_threshold=10,
            quantity_on_hand=1,
        )

        _make_reservation(tenant, asset_general, admin, status=Reservation.Status.APPROVED)
        _make_reservation(
            tenant, asset_project, admin, status=Reservation.Status.APPROVED, start_in_days=30
        )

        _login(client, tenant, admin)
        response = client.get(URL)
        assert response.status_code == 200, response.content
        body = response.json()

        # Retired asset excluded from totals; general-pool + project asset,
        # plus the StockItemFactory's own (general-pool) consumable asset,
        # are counted (3 non-retired assets total).
        assert sum(row["count"] for row in body["totals_by_category"]) == 3
        assert body["currently_out"] == 2
        assert body["overdue"] == 1
        assert body["low_stock"] == 1
        # Only the reservation within the 7-day default window counts.
        assert body["upcoming_reservations"] == 1
        assert body["upcoming_reservations_window_days"] == 7

        project_rows = {row["project_id"]: row["count"] for row in body["per_project_allocation"]}
        # General pool: asset_general + the StockItemFactory's own asset.
        assert project_rows[None] == 2
        assert project_rows[project.id] == 1

    def test_project_lead_sees_only_their_project(self, client):
        tenant = TenantFactory()
        category = CategoryFactory(tenant=tenant)
        project_a = ProjectFactory(tenant=tenant)
        project_b = ProjectFactory(tenant=tenant)

        lead = UserFactory(tenant=tenant)
        Membership.all_objects.filter(user=lead, project__isnull=True).delete()
        add_project_membership(lead, project_a, ROLE_PROJECT_LEAD)

        asset_a = AssetFactory(tenant=tenant, category=category, project=project_a)
        asset_b = AssetFactory(tenant=tenant, category=category, project=project_b)

        _make_checkout(tenant, asset_a, lead)
        _make_checkout(tenant, asset_b, lead)

        _login(client, tenant, lead)
        response = client.get(URL)
        assert response.status_code == 200, response.content
        body = response.json()

        assert body["currently_out"] == 1
        project_rows = {row["project_id"]: row["count"] for row in body["per_project_allocation"]}
        assert project_rows == {project_a.id: 1}
        assert None not in project_rows
        assert project_b.id not in project_rows

    def test_cross_tenant_isolation(self, client):
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        admin_a = UserFactory(tenant=tenant_a)
        upgrade_tenant_wide_role(admin_a, ROLE_ADMIN)
        admin_b = UserFactory(tenant=tenant_b)
        upgrade_tenant_wide_role(admin_b, ROLE_ADMIN)

        category_b = CategoryFactory(tenant=tenant_b)
        AssetFactory(tenant=tenant_b, category=category_b)
        AssetFactory(tenant=tenant_b, category=category_b)

        _login(client, tenant_a, admin_a)
        response = client.get(URL)
        assert response.status_code == 200, response.content
        assert response.json()["totals_by_category"] == []


class TestDashboardSummaryCaching:
    def test_response_is_cached_within_ttl(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = CategoryFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=category)
        _make_checkout(tenant, asset, admin)

        _login(client, tenant, admin)
        first = client.get(URL).json()
        assert first["currently_out"] == 1

        # A DIRECT ORM check-in (bypassing the API/invalidation hook) --
        # simulates "some other write path changed the data"; the cached
        # response should still be served until the TTL/version changes.
        with tenant_context(tenant.id):
            Checkout.objects.filter(asset=asset).update(checked_in_at=timezone.now())

        second = client.get(URL).json()
        assert second["currently_out"] == 1  # still cached, not yet invalidated

    def test_invalidate_tenant_dashboard_busts_the_cache(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = CategoryFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=category)
        _make_checkout(tenant, asset, admin)

        _login(client, tenant, admin)
        first = client.get(URL).json()
        assert first["currently_out"] == 1

        with tenant_context(tenant.id):
            Checkout.objects.filter(asset=asset).update(checked_in_at=timezone.now())
        invalidate_tenant_dashboard(tenant.id)

        second = client.get(URL).json()
        assert second["currently_out"] == 0

    def test_checkout_via_api_invalidates_immediately(self, client):
        """End-to-end proof of the T5.5 exit criterion: a checkout made
        through the real endpoint is visible on the dashboard right away,
        no TTL wait needed."""
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = CategoryFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=category, status="available")

        _login(client, tenant, admin)

        before = client.get(URL).json()
        assert before["currently_out"] == 0

        checkout_resp = client.post(
            "/api/v1/checkouts/",
            {
                "asset": asset.id,
                "user": admin.id,
                "due_at": (timezone.now() + timedelta(days=1)).isoformat(),
            },
            content_type="application/json",
        )
        assert checkout_resp.status_code == 201, checkout_resp.content

        after = client.get(URL).json()
        assert after["currently_out"] == 1

    def test_scope_key_differs_between_tenant_wide_and_project_scoped(self):
        key_all = summary_cache_key(1, tenant_wide=True, project_ids=frozenset())
        key_scoped = summary_cache_key(1, tenant_wide=False, project_ids=frozenset({7}))
        key_none = summary_cache_key(1, tenant_wide=False, project_ids=frozenset())
        assert len({key_all, key_scoped, key_none}) == 3


class TestDashboardSummaryQueryBudget:
    def test_query_count_bounded_and_independent_of_row_count(
        self, client, django_assert_max_num_queries
    ):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = CategoryFactory(tenant=tenant)
        project = ProjectFactory(tenant=tenant)

        with tenant_context(tenant.id):
            for _i in range(15):
                asset = AssetFactory(tenant=tenant, category=category, project=project)
                Checkout.objects.create(
                    tenant=tenant,
                    asset=asset,
                    user=admin,
                    checked_out_at=timezone.now() - timedelta(hours=1),
                    due_at=timezone.now() + timedelta(hours=24),
                )

        _login(client, tenant, admin)
        with django_assert_max_num_queries(20):
            response = client.get(URL)
        assert response.status_code == 200, response.content
        small_count = response.json()["currently_out"]

        with tenant_context(tenant.id):
            for _i in range(15, 45):
                asset = AssetFactory(tenant=tenant, category=category, project=project)
                Checkout.objects.create(
                    tenant=tenant,
                    asset=asset,
                    user=admin,
                    checked_out_at=timezone.now() - timedelta(hours=1),
                    due_at=timezone.now() + timedelta(hours=24),
                )
        invalidate_tenant_dashboard(tenant.id)

        with django_assert_max_num_queries(20):
            response = client.get(URL)
        assert response.status_code == 200, response.content
        assert response.json()["currently_out"] == small_count + 30
