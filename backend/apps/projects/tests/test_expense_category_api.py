"""`GET /api/v1/expense-categories` (frontend follow-up, `docs/tasks/
M7-project-grants.md`): read-only, tenant-scoped reference data for the
expense form's category picker. Modeled on `apps.catalog.api.TagViewSet` —
covers tenant isolation, the `project.view`-gated read (non-financial
reference data, same tier as tags/locations), and the seeded-default-set +
`?include_inactive=` contract.
"""

from __future__ import annotations

import pytest

from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
    ExpenseCategoryFactory,
    ProjectFactory,
    TenantFactory,
    UserFactory,
    add_project_membership,
    upgrade_tenant_wide_role,
)
from apps.rbac.models import Membership
from apps.rbac.permission_keys import ROLE_ADMIN, ROLE_PROJECT_LEAD
from apps.tenancy.context import tenant_context

pytestmark = pytest.mark.django_db

DEFAULT_SEEDED_NAMES = {
    "Equipment",
    "Consumables",
    "Services",
    "Software",
    "Travel",
    "Shipping",
    "Other",
}


def _login(client, tenant, user):
    response = client.post(
        "/api/v1/auth/login",
        {"tenant": tenant.slug, "email": user.email, "password": DEFAULT_TEST_PASSWORD},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return response


class TestExpenseCategoryList:
    def test_the_7_seeded_defaults_are_returned_ordered_by_name(self, client):
        """Every `Tenant` auto-seeds the 7 default categories on creation
        (`apps.projects.signals.seed_expense_categories`) — a brand-new
        tenant's list should show exactly those, alphabetically."""
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        _login(client, tenant, admin)

        response = client.get("/api/v1/expense-categories/")

        assert response.status_code == 200, response.content
        results = response.json()["results"]
        names = [row["name"] for row in results]
        assert set(names) == DEFAULT_SEEDED_NAMES
        assert names == sorted(names)
        assert all(row["is_active"] for row in results)

    def test_tenant_a_never_sees_tenant_bs_categories(self, client):
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        admin_a = UserFactory(tenant=tenant_a)
        upgrade_tenant_wide_role(admin_a, ROLE_ADMIN)
        ExpenseCategoryFactory(tenant=tenant_b, name="Tenant B Only Category")
        _login(client, tenant_a, admin_a)

        response = client.get("/api/v1/expense-categories/")

        assert response.status_code == 200, response.content
        names = {row["name"] for row in response.json()["results"]}
        assert names == DEFAULT_SEEDED_NAMES
        assert "Tenant B Only Category" not in names

    def test_member_can_list_categories(self, client):
        """Non-financial reference data, gated by `project.view` (Member
        holds it tenant-wide, `docs/rbac.md` §3 additions matrix) — NOT the
        `expense.view` financial boundary this app enforces elsewhere."""
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)  # default Member role, tenant-wide
        _login(client, tenant, member)

        response = client.get("/api/v1/expense-categories/")
        assert response.status_code == 200, response.content

    def test_pure_project_scoped_lead_can_list_categories(self, client):
        """Code-review finding: gating on `apps.catalog.permissions.
        TenantWideView` (tenant-wide `project.view` grant ONLY) would 403 a
        PURE project-scoped Project Lead — a user whose ONLY membership is
        a project-scoped `project.view` grant, no tenant-wide Member role at
        all — losing the expense-form category picker. `apps.projects.
        permissions.ExpenseCategoryPermission` uses `user_has_permission_in_
        any_scope` instead, so this user must still be allowed (`Expense
        Category` has no `project_id` of its own to scope against).
        """
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        lead = UserFactory(tenant=tenant)  # auto-creates a tenant-wide Member membership
        with tenant_context(tenant.id):
            # Remove the auto-created tenant-wide membership so this user's
            # ONLY grant is the project-scoped one added below — otherwise
            # Member's own tenant-wide `project.view` would mask the bug
            # this test exists to catch.
            Membership.all_objects.filter(user=lead, project__isnull=True).delete()
        add_project_membership(lead, project, ROLE_PROJECT_LEAD)
        _login(client, tenant, lead)

        response = client.get("/api/v1/expense-categories/")

        assert response.status_code == 200, response.content
        names = {row["name"] for row in response.json()["results"]}
        assert names == DEFAULT_SEEDED_NAMES

    def test_unauthenticated_request_is_denied(self, client):
        response = client.get("/api/v1/expense-categories/")
        assert response.status_code in (401, 403)

    def test_inactive_categories_excluded_by_default_and_included_on_request(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        ExpenseCategoryFactory(tenant=tenant, name="Retired Category", is_active=False)
        _login(client, tenant, admin)

        default_response = client.get("/api/v1/expense-categories/")
        default_names = {row["name"] for row in default_response.json()["results"]}
        assert "Retired Category" not in default_names

        included_response = client.get("/api/v1/expense-categories/?include_inactive=true")
        included_names = {row["name"] for row in included_response.json()["results"]}
        assert "Retired Category" in included_names
