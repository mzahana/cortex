"""`GET /api/v1/roles` — read-only role-catalog gap fill so the frontend can
populate a role picker when granting a Membership (`POST /api/v1/memberships`
needs a real, tenant-specific `role` id; there was previously no way to
discover one). Proves: any `user.manage` grant (tenant-wide OR
project-scoped) can list; a Member/Viewer with no `user.manage` grant at all
cannot; and tenant isolation (only the caller's own tenant's 4 seeded roles
ever come back, never another tenant's).
"""

from __future__ import annotations

import pytest

from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
    ProjectFactory,
    TenantFactory,
    UserFactory,
    add_project_membership,
    upgrade_tenant_wide_role,
)
from apps.rbac.permission_keys import ROLE_ADMIN, ROLE_MEMBER, ROLE_PROJECT_LEAD, ROLE_VIEWER

pytestmark = pytest.mark.django_db


def _login(client, tenant, user):
    response = client.post(
        "/api/v1/auth/login",
        {"tenant": tenant.slug, "email": user.email, "password": DEFAULT_TEST_PASSWORD},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return response


class TestRoleListRBAC:
    def test_admin_tenant_wide_can_list_all_four_system_roles(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        _login(client, tenant, admin)

        response = client.get("/api/v1/roles/")

        assert response.status_code == 200, response.content
        keys = {row["key"] for row in response.json()["results"]}
        assert keys == {ROLE_ADMIN, ROLE_PROJECT_LEAD, ROLE_MEMBER, ROLE_VIEWER}

    def test_project_scoped_grant_can_also_list(self, client):
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, project, ROLE_PROJECT_LEAD)
        _login(client, tenant, lead)

        response = client.get("/api/v1/roles/")

        assert response.status_code == 200, response.content

    def test_member_with_no_user_manage_grant_is_denied(self, client):
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        member = UserFactory(tenant=tenant)
        add_project_membership(member, project, ROLE_MEMBER)
        _login(client, tenant, member)

        response = client.get("/api/v1/roles/")

        assert response.status_code == 403

    def test_viewer_is_denied(self, client):
        tenant = TenantFactory()
        viewer = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(viewer, ROLE_VIEWER)
        _login(client, tenant, viewer)

        response = client.get("/api/v1/roles/")

        assert response.status_code == 403


class TestRoleListTenantIsolation:
    def test_only_the_callers_own_tenant_roles_come_back(self, client):
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        admin_a = UserFactory(tenant=tenant_a)
        upgrade_tenant_wide_role(admin_a, ROLE_ADMIN)
        _login(client, tenant_a, admin_a)

        response = client.get("/api/v1/roles/")

        assert response.status_code == 200, response.content
        for row in response.json()["results"]:
            # Every returned role id belongs to tenant_a's own seeded set --
            # proven indirectly (tenant-scoped manager, R4) by count: exactly
            # the 4 system roles, never tenant_b's 4 as well (would be 8).
            assert row["key"] in {ROLE_ADMIN, ROLE_PROJECT_LEAD, ROLE_MEMBER, ROLE_VIEWER}
        assert len(response.json()["results"]) == 4
