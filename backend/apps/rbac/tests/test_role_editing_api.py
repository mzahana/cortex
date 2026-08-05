"""Admin-editable role permission sets + tenant-authored custom roles
(docs/rbac.md §6).

Proves the four things this feature can get dangerously wrong:
1. The motivating case works — an Admin can hand Project Lead a permission
   the shipped matrix withholds (`category.manage`), and it takes effect for
   a real request by a real lead.
2. Only an Admin can do it — a ProjectLead holds `user.manage` *scoped* and
   can still READ the role catalog, but must never be able to edit the rules
   that define their own power.
3. The lockout guardrail holds — no edit may leave the tenant with nobody
   able to administer it (self-hosted Cortex has no break-glass path).
4. Re-running the idempotent seed does NOT resurrect grants an admin removed
   (`Role.is_customized`), which would silently undo their decision.
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
from apps.rbac.models import Permission, Role, RolePermission
from apps.rbac.permission_keys import (
    CATEGORY_MANAGE,
    ROLE_ADMIN,
    ROLE_MEMBER,
    ROLE_PROJECT_LEAD,
    TENANT_MANAGE,
    USER_MANAGE,
)
from apps.rbac.seed import seed_roles_for_tenant

pytestmark = pytest.mark.django_db


def _login(client, tenant, user):
    response = client.post(
        "/api/v1/auth/login",
        {"tenant": tenant.slug, "email": user.email, "password": DEFAULT_TEST_PASSWORD},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return response


def _role(tenant, key: str) -> Role:
    return Role.all_objects.get(tenant=tenant, key=key)


def _keys(role: Role) -> set[str]:
    return set(
        RolePermission.all_objects.filter(role=role).values_list("permission__key", flat=True)
    )


class TestAdminEditsRolePermissions:
    def test_admin_can_grant_category_manage_to_project_lead(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        _login(client, tenant, admin)

        lead_role = _role(tenant, ROLE_PROJECT_LEAD)
        wanted = sorted(_keys(lead_role) | {CATEGORY_MANAGE})

        response = client.patch(
            f"/api/v1/roles/{lead_role.id}/",
            {"permission_keys": wanted},
            content_type="application/json",
        )

        assert response.status_code == 200, response.content
        assert CATEGORY_MANAGE in response.json()["permission_keys"]
        assert response.json()["is_customized"] is True
        assert CATEGORY_MANAGE in _keys(lead_role)

    def test_the_edit_actually_takes_effect_for_the_lead(self, client):
        """The point of the whole feature: a lead who previously got 403 on
        `POST /categories` can create one after the grant."""
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, project, ROLE_PROJECT_LEAD)

        # Before: denied.
        _login(client, tenant, lead)
        before = client.post(
            "/api/v1/categories/", {"name": "Bench PSUs"}, content_type="application/json"
        )
        assert before.status_code == 403, before.content
        client.post("/api/v1/auth/logout")

        # Admin grants `category.manage` to the Project Lead role. NOTE the
        # role is only effective through a PROJECT-scoped membership, but
        # `category.manage` is a tenant-wide-evaluated key
        # (`apps.catalog.permissions`), so this also needs a tenant-wide
        # membership to reach — granted here as an ordinary Member+lead
        # combination would be in practice.
        _login(client, tenant, admin)
        lead_role = _role(tenant, ROLE_PROJECT_LEAD)
        client.patch(
            f"/api/v1/roles/{lead_role.id}/",
            {"permission_keys": sorted(_keys(lead_role) | {CATEGORY_MANAGE})},
            content_type="application/json",
        )
        # Give the lead a tenant-wide membership of the (now edited) role so
        # the tenant-wide-evaluated key is actually in scope for them.
        upgrade_tenant_wide_role(lead, ROLE_PROJECT_LEAD)
        client.post("/api/v1/auth/logout")

        _login(client, tenant, lead)
        after = client.post(
            "/api/v1/categories/", {"name": "Bench PSUs"}, content_type="application/json"
        )

        assert after.status_code == 201, after.content

    def test_removing_a_permission_revokes_it(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        _login(client, tenant, admin)

        member_role = _role(tenant, ROLE_MEMBER)
        remaining = sorted(_keys(member_role) - {"reservation.create"})

        response = client.patch(
            f"/api/v1/roles/{member_role.id}/",
            {"permission_keys": remaining},
            content_type="application/json",
        )

        assert response.status_code == 200, response.content
        assert "reservation.create" not in _keys(member_role)

    def test_unknown_permission_key_is_rejected(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        _login(client, tenant, admin)

        response = client.patch(
            f"/api/v1/roles/{_role(tenant, ROLE_MEMBER).id}/",
            {"permission_keys": ["asset.view", "asset.teleport"]},
            content_type="application/json",
        )

        assert response.status_code == 400, response.content


class TestRoleEditingRBAC:
    def test_project_lead_can_read_but_not_edit(self, client):
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, project, ROLE_PROJECT_LEAD)
        _login(client, tenant, lead)

        assert client.get("/api/v1/roles/").status_code == 200

        lead_role = _role(tenant, ROLE_PROJECT_LEAD)
        response = client.patch(
            f"/api/v1/roles/{lead_role.id}/",
            {"permission_keys": sorted(_keys(lead_role) | {TENANT_MANAGE})},
            content_type="application/json",
        )

        assert response.status_code == 403, response.content
        assert TENANT_MANAGE not in _keys(lead_role)

    def test_member_cannot_even_read(self, client):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(member, ROLE_MEMBER)
        _login(client, tenant, member)

        assert client.get("/api/v1/roles/").status_code == 403

    def test_cross_tenant_role_id_is_not_editable(self, client):
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        admin_a = UserFactory(tenant=tenant_a)
        upgrade_tenant_wide_role(admin_a, ROLE_ADMIN)
        _login(client, tenant_a, admin_a)

        foreign_role = _role(tenant_b, ROLE_MEMBER)
        response = client.patch(
            f"/api/v1/roles/{foreign_role.id}/",
            {"permission_keys": ["asset.view"]},
            content_type="application/json",
        )

        assert response.status_code == 404, response.content
        assert _keys(foreign_role) != {"asset.view"}


class TestCustomRoles:
    def test_create_and_delete_a_custom_role(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        _login(client, tenant, admin)

        created = client.post(
            "/api/v1/roles/",
            {
                "key": "Lab Tech",
                "name": "Lab Tech",
                "permission_keys": ["asset.view", "asset.edit", CATEGORY_MANAGE],
            },
            content_type="application/json",
        )

        assert created.status_code == 201, created.content
        body = created.json()
        assert body["key"] == "lab-tech"
        assert body["is_system"] is False
        assert set(body["permission_keys"]) == {"asset.view", "asset.edit", CATEGORY_MANAGE}

        deleted = client.delete(f"/api/v1/roles/{body['id']}/")
        assert deleted.status_code == 204, deleted.content

    def test_name_only_create_derives_the_key(self, client):
        """The UI collects a human name and nothing else — a blank `key` must
        never reach the DB."""
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        _login(client, tenant, admin)

        created = client.post(
            "/api/v1/roles/",
            {"name": "Bench Tech", "permission_keys": ["asset.view"]},
            content_type="application/json",
        )

        assert created.status_code == 201, created.content
        assert created.json()["key"] == "bench-tech"

    def test_system_role_cannot_be_deleted(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        _login(client, tenant, admin)

        response = client.delete(f"/api/v1/roles/{_role(tenant, ROLE_MEMBER).id}/")

        assert response.status_code == 400, response.content

    def test_role_in_use_cannot_be_deleted(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        _login(client, tenant, admin)

        created = client.post(
            "/api/v1/roles/",
            {"key": "temp", "name": "Temp", "permission_keys": ["asset.view"]},
            content_type="application/json",
        )
        role_id = created.json()["id"]
        holder = UserFactory(tenant=tenant)
        granted = client.post(
            "/api/v1/memberships/",
            {"user": holder.id, "role": role_id, "project": None},
            content_type="application/json",
        )
        assert granted.status_code == 201, granted.content

        response = client.delete(f"/api/v1/roles/{role_id}/")

        assert response.status_code == 400, response.content


class TestResetToDefaults:
    def test_reset_restores_the_shipped_matrix_and_clears_customized(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        _login(client, tenant, admin)

        lead_role = _role(tenant, ROLE_PROJECT_LEAD)
        defaults = _keys(lead_role)
        client.patch(
            f"/api/v1/roles/{lead_role.id}/",
            {"permission_keys": ["asset.view"]},
            content_type="application/json",
        )
        assert _keys(lead_role) == {"asset.view"}

        response = client.post(f"/api/v1/roles/{lead_role.id}/reset/")

        assert response.status_code == 200, response.content
        assert _keys(lead_role) == defaults
        lead_role.refresh_from_db()
        assert lead_role.is_customized is False


class TestLockoutGuardrail:
    def test_cannot_strip_admin_role_of_tenant_administration(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        _login(client, tenant, admin)

        admin_role = _role(tenant, ROLE_ADMIN)
        response = client.patch(
            f"/api/v1/roles/{admin_role.id}/",
            {"permission_keys": ["asset.view"]},
            content_type="application/json",
        )

        assert response.status_code == 400, response.content
        # And the transaction rolled back — the tenant still has an admin.
        assert TENANT_MANAGE in _keys(admin_role)
        assert USER_MANAGE in _keys(admin_role)

    def test_allowed_when_another_admin_still_qualifies(self, client):
        """The guardrail is about the TENANT keeping an administrator, not
        about any particular role keeping its grants: with a second admin
        holding a custom admin-capable role, editing the original one is
        fine."""
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        _login(client, tenant, admin)

        backup_role = client.post(
            "/api/v1/roles/",
            {
                "key": "backup-admin",
                "name": "Backup Admin",
                "permission_keys": [TENANT_MANAGE, USER_MANAGE],
            },
            content_type="application/json",
        ).json()
        second = UserFactory(tenant=tenant)
        granted = client.post(
            "/api/v1/memberships/",
            {"user": second.id, "role": backup_role["id"], "project": None},
            content_type="application/json",
        )
        assert granted.status_code == 201, granted.content

        admin_role = _role(tenant, ROLE_ADMIN)
        response = client.patch(
            f"/api/v1/roles/{admin_role.id}/",
            {"permission_keys": ["asset.view"]},
            content_type="application/json",
        )

        assert response.status_code == 200, response.content


class TestSeedDoesNotResurrectRemovedGrants:
    def test_customized_role_survives_a_reseed(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        _login(client, tenant, admin)

        member_role = _role(tenant, ROLE_MEMBER)
        reduced = sorted(_keys(member_role) - {"issue.report"})
        client.patch(
            f"/api/v1/roles/{member_role.id}/",
            {"permission_keys": reduced},
            content_type="application/json",
        )

        # The exact call a migration backfill / management command makes.
        seed_roles_for_tenant(
            tenant=tenant,
            role_model=Role,
            permission_model=Permission,
            role_permission_model=RolePermission,
        )

        assert "issue.report" not in _keys(member_role)

    def test_untouched_role_is_still_reseeded_normally(self, client):
        tenant = TenantFactory()
        member_role = _role(tenant, ROLE_MEMBER)
        defaults = _keys(member_role)
        RolePermission.all_objects.filter(role=member_role, permission__key="issue.report").delete()

        seed_roles_for_tenant(
            tenant=tenant,
            role_model=Role,
            permission_model=Permission,
            role_permission_model=RolePermission,
        )

        assert _keys(member_role) == defaults
