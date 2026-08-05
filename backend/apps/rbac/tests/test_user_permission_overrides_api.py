"""Per-user permission overrides — `GET/PUT /api/v1/users/{id}/permissions`
(docs/rbac.md §6).

The layer an Admin reaches for when ONE person needs a deviation from their
role, without authoring a whole custom role. Proves:
- a GRANT genuinely adds the capability for a real request by that user;
- a DENY genuinely removes it, and beats the role that grants it;
- overrides are tenant-wide by construction — a DENY reaches a permission
  the user only held through a PROJECT-scoped membership too;
- only a tenant Admin may read or write them (never a ProjectLead);
- tenant isolation on the target user id;
- the lockout guardrail: an Admin cannot DENY the last administrator.
"""

from __future__ import annotations

import pytest

from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
    CategoryFactory,
    ProjectFactory,
    TenantFactory,
    UserFactory,
    add_project_membership,
    upgrade_tenant_wide_role,
)
from apps.rbac.models import UserPermissionOverride
from apps.rbac.permission_keys import (
    ASSET_VIEW,
    CATEGORY_MANAGE,
    ROLE_ADMIN,
    ROLE_MEMBER,
    ROLE_PROJECT_LEAD,
    TENANT_MANAGE,
    USER_MANAGE,
)

pytestmark = pytest.mark.django_db


def _login(client, tenant, user):
    response = client.post(
        "/api/v1/auth/login",
        {"tenant": tenant.slug, "email": user.email, "password": DEFAULT_TEST_PASSWORD},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return response


def _admin(tenant):
    admin = UserFactory(tenant=tenant)
    upgrade_tenant_wide_role(admin, ROLE_ADMIN)
    return admin


class TestGrantOverride:
    def test_grant_adds_a_capability_the_role_withholds(self, client):
        tenant = TenantFactory()
        admin = _admin(tenant)
        member = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(member, ROLE_MEMBER)

        _login(client, tenant, admin)
        response = client.put(
            f"/api/v1/users/{member.id}/permissions",
            {"overrides": {CATEGORY_MANAGE: "grant"}},
            content_type="application/json",
        )
        assert response.status_code == 200, response.content
        assert CATEGORY_MANAGE in response.json()["effective_permission_keys"]
        client.post("/api/v1/auth/logout")

        _login(client, tenant, member)
        created = client.post(
            "/api/v1/categories/", {"name": "Optics"}, content_type="application/json"
        )

        assert created.status_code == 201, created.content

    def test_me_reports_the_granted_key(self, client):
        tenant = TenantFactory()
        admin = _admin(tenant)
        member = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(member, ROLE_MEMBER)

        _login(client, tenant, admin)
        client.put(
            f"/api/v1/users/{member.id}/permissions",
            {"overrides": {CATEGORY_MANAGE: "grant"}},
            content_type="application/json",
        )
        client.post("/api/v1/auth/logout")

        _login(client, tenant, member)
        me = client.get("/api/v1/me")

        assert CATEGORY_MANAGE in me.json()["permissions"]


class TestDenyOverride:
    def test_deny_removes_a_capability_the_role_grants(self, client):
        tenant = TenantFactory()
        admin = _admin(tenant)
        member = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(member, ROLE_MEMBER)
        CategoryFactory(tenant=tenant)

        _login(client, tenant, member)
        assert client.get("/api/v1/assets/").status_code == 200
        client.post("/api/v1/auth/logout")

        _login(client, tenant, admin)
        client.put(
            f"/api/v1/users/{member.id}/permissions",
            {"overrides": {ASSET_VIEW: "deny"}},
            content_type="application/json",
        )
        client.post("/api/v1/auth/logout")

        _login(client, tenant, member)
        assert client.get("/api/v1/assets/").status_code == 403

    def test_deny_beats_a_project_scoped_role_grant(self, client):
        """Overrides are tenant-wide: a DENY has to reach a permission the
        user only holds through a project-scoped membership, or "never allow"
        would be a lie for exactly the users most likely to need it."""
        tenant = TenantFactory()
        admin = _admin(tenant)
        project = ProjectFactory(tenant=tenant)
        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, project, ROLE_PROJECT_LEAD)

        _login(client, tenant, admin)
        client.put(
            f"/api/v1/users/{lead.id}/permissions",
            {"overrides": {"label.generate": "deny"}},
            content_type="application/json",
        )
        client.post("/api/v1/auth/logout")

        _login(client, tenant, lead)
        response = client.post(
            "/api/v1/labels/generate",
            {"asset_ids": [1], "template": "single"},
            content_type="application/json",
        )

        assert response.status_code == 403, response.content

    def test_deny_wins_over_a_grant_for_the_same_key(self, client):
        tenant = TenantFactory()
        admin = _admin(tenant)
        member = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(member, ROLE_MEMBER)

        _login(client, tenant, admin)
        # The unique constraint means one row per (user, key); this asserts
        # the LAST write wins cleanly rather than accumulating both.
        client.put(
            f"/api/v1/users/{member.id}/permissions",
            {"overrides": {CATEGORY_MANAGE: "grant"}},
            content_type="application/json",
        )
        response = client.put(
            f"/api/v1/users/{member.id}/permissions",
            {"overrides": {CATEGORY_MANAGE: "deny"}},
            content_type="application/json",
        )

        assert response.status_code == 200, response.content
        assert CATEGORY_MANAGE not in response.json()["effective_permission_keys"]
        assert UserPermissionOverride.all_objects.filter(user=member).count() == 1


class TestReplaceSemantics:
    def test_put_replaces_the_whole_set(self, client):
        tenant = TenantFactory()
        admin = _admin(tenant)
        member = UserFactory(tenant=tenant)

        _login(client, tenant, admin)
        client.put(
            f"/api/v1/users/{member.id}/permissions",
            {"overrides": {CATEGORY_MANAGE: "grant", ASSET_VIEW: "deny"}},
            content_type="application/json",
        )
        response = client.put(
            f"/api/v1/users/{member.id}/permissions",
            {"overrides": {CATEGORY_MANAGE: "grant"}},
            content_type="application/json",
        )

        assert response.json()["overrides"] == {CATEGORY_MANAGE: "grant"}

    def test_empty_overrides_clears_everything(self, client):
        tenant = TenantFactory()
        admin = _admin(tenant)
        member = UserFactory(tenant=tenant)

        _login(client, tenant, admin)
        client.put(
            f"/api/v1/users/{member.id}/permissions",
            {"overrides": {ASSET_VIEW: "deny"}},
            content_type="application/json",
        )
        response = client.put(
            f"/api/v1/users/{member.id}/permissions",
            {"overrides": {}},
            content_type="application/json",
        )

        assert response.json()["overrides"] == {}
        assert not UserPermissionOverride.all_objects.filter(user=member).exists()

    def test_bad_effect_is_rejected(self, client):
        tenant = TenantFactory()
        admin = _admin(tenant)
        member = UserFactory(tenant=tenant)
        _login(client, tenant, admin)

        response = client.put(
            f"/api/v1/users/{member.id}/permissions",
            {"overrides": {ASSET_VIEW: "maybe"}},
            content_type="application/json",
        )

        assert response.status_code == 400, response.content


class TestOverrideRBAC:
    def test_project_lead_cannot_read_or_write(self, client):
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, project, ROLE_PROJECT_LEAD)
        target = UserFactory(tenant=tenant)
        _login(client, tenant, lead)

        assert client.get(f"/api/v1/users/{target.id}/permissions").status_code == 403
        assert (
            client.put(
                f"/api/v1/users/{target.id}/permissions",
                {"overrides": {TENANT_MANAGE: "grant"}},
                content_type="application/json",
            ).status_code
            == 403
        )
        assert not UserPermissionOverride.all_objects.filter(user=target).exists()

    def test_member_cannot_grant_themselves_anything(self, client):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(member, ROLE_MEMBER)
        _login(client, tenant, member)

        response = client.put(
            f"/api/v1/users/{member.id}/permissions",
            {"overrides": {TENANT_MANAGE: "grant"}},
            content_type="application/json",
        )

        assert response.status_code == 403, response.content

    def test_cross_tenant_user_id_is_not_reachable(self, client):
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        admin_a = _admin(tenant_a)
        victim_b = UserFactory(tenant=tenant_b)
        _login(client, tenant_a, admin_a)

        assert client.get(f"/api/v1/users/{victim_b.id}/permissions").status_code == 404
        response = client.put(
            f"/api/v1/users/{victim_b.id}/permissions",
            {"overrides": {ASSET_VIEW: "deny"}},
            content_type="application/json",
        )
        assert response.status_code == 404, response.content
        assert not UserPermissionOverride.all_objects.filter(user=victim_b).exists()


class TestLockoutGuardrail:
    def test_cannot_deny_the_last_admin_out_of_administering(self, client):
        tenant = TenantFactory()
        admin = _admin(tenant)
        _login(client, tenant, admin)

        response = client.put(
            f"/api/v1/users/{admin.id}/permissions",
            {"overrides": {TENANT_MANAGE: "deny", USER_MANAGE: "deny"}},
            content_type="application/json",
        )

        assert response.status_code == 400, response.content
        assert not UserPermissionOverride.all_objects.filter(user=admin).exists()

    def test_allowed_when_a_second_admin_remains(self, client):
        tenant = TenantFactory()
        admin = _admin(tenant)
        second = _admin(tenant)
        _login(client, tenant, admin)

        response = client.put(
            f"/api/v1/users/{second.id}/permissions",
            {"overrides": {TENANT_MANAGE: "deny"}},
            content_type="application/json",
        )

        assert response.status_code == 200, response.content
