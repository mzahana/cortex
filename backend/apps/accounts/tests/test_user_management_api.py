"""`GET/POST /api/v1/users` -- the "create/discover a user" gap fill (see
`apps.accounts.services` module docstring for the full gap analysis this
task closes).

Covers:
- Admin-only create, ProjectLead/Member/Viewer denied.
- Clean 400 (not 500) on a duplicate email within the SAME tenant; the SAME
  email in a DIFFERENT tenant is allowed (per-tenant uniqueness, R4).
- The returned one-time password actually works for a subsequent login.
- The created user has no memberships beyond the automatic default
  tenant-wide Member membership every new `User` gets
  (`apps.rbac.signals.assign_default_membership`, docs/rbac.md §5: "New
  users default to Member; elevated roles are granted explicitly") --
  NOT zero permissions, but zero *elevated* ones: this endpoint's two-step
  design is "create the account, then grant an ADDITIONAL role" via the
  existing Membership API, same as every other `UserFactory()`-created user
  in this test suite.
- The `user.manage` audit entry for creation never contains the password.
- List: Admin sees all tenant users; a ProjectLead with a project-scoped
  `user.manage` grant can also list (any-scope rule); a Member/Viewer with
  no `user.manage` grant anywhere gets 403; `?search=` filters; cross-tenant
  isolation.
"""

from __future__ import annotations

import json

import pytest

from apps.audit.models import AuditLog
from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
    ProjectFactory,
    TenantFactory,
    UserFactory,
    add_project_membership,
    upgrade_tenant_wide_role,
)
from apps.rbac.models import Membership
from apps.rbac.permission_keys import ROLE_ADMIN, ROLE_PROJECT_LEAD, ROLE_VIEWER
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


class TestUserCreate:
    def test_admin_can_create_user(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)

        _login(client, tenant, admin)
        response = client.post(
            "/api/v1/users/",
            data=json.dumps({"email": "newperson@example.test", "name": "New Person"}),
            content_type="application/json",
        )
        assert response.status_code == 201, response.content
        body = response.json()
        assert body["email"] == "newperson@example.test"
        assert body["name"] == "New Person"
        assert "id" in body
        assert "password" in body and body["password"]

    def test_project_lead_cannot_create_user(self, client):
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, project, ROLE_PROJECT_LEAD)

        _login(client, tenant, lead)
        response = client.post(
            "/api/v1/users/",
            data=json.dumps({"email": "x@example.test", "name": "X"}),
            content_type="application/json",
        )
        assert response.status_code == 403

    def test_plain_member_cannot_create_user(self, client):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)  # default: tenant-wide Member

        _login(client, tenant, member)
        response = client.post(
            "/api/v1/users/",
            data=json.dumps({"email": "y@example.test", "name": "Y"}),
            content_type="application/json",
        )
        assert response.status_code == 403

    def test_viewer_cannot_create_user(self, client):
        tenant = TenantFactory()
        viewer = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(viewer, ROLE_VIEWER)

        _login(client, tenant, viewer)
        response = client.post(
            "/api/v1/users/",
            data=json.dumps({"email": "z@example.test", "name": "Z"}),
            content_type="application/json",
        )
        assert response.status_code == 403

    def test_duplicate_email_within_same_tenant_is_rejected_cleanly(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        UserFactory(tenant=tenant, email="dupe@example.test")

        _login(client, tenant, admin)
        response = client.post(
            "/api/v1/users/",
            data=json.dumps({"email": "dupe@example.test", "name": "Dupe"}),
            content_type="application/json",
        )
        assert response.status_code == 400, response.content
        assert response.status_code != 500

    def test_same_email_allowed_in_a_different_tenant(self, client):
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        UserFactory(tenant=tenant_a, email="shared@example.test")
        admin_b = UserFactory(tenant=tenant_b)
        upgrade_tenant_wide_role(admin_b, ROLE_ADMIN)

        _login(client, tenant_b, admin_b)
        response = client.post(
            "/api/v1/users/",
            data=json.dumps({"email": "shared@example.test", "name": "Shared"}),
            content_type="application/json",
        )
        assert response.status_code == 201, response.content

    def test_generated_password_actually_works_for_login(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)

        _login(client, tenant, admin)
        create_response = client.post(
            "/api/v1/users/",
            data=json.dumps({"email": "loginworks@example.test", "name": "Login Works"}),
            content_type="application/json",
        )
        assert create_response.status_code == 201, create_response.content
        password = create_response.json()["password"]

        # Fresh client -- log out the admin's session, or just use a new
        # client instance so cookies don't collide.
        from django.test import Client

        fresh_client = Client()
        login_response = fresh_client.post(
            "/api/v1/auth/login",
            {"tenant": tenant.slug, "email": "loginworks@example.test", "password": password},
            content_type="application/json",
        )
        assert login_response.status_code == 200, login_response.content

    def test_created_user_has_only_the_automatic_default_member_membership(self, client):
        """Two-step design: create the account, then grant a role via the
        EXISTING `POST /api/v1/memberships` endpoint. A freshly created user
        holds nothing beyond the automatic tenant-wide Member membership
        every `User` gets on creation (`apps.rbac.signals.
        assign_default_membership`, docs/rbac.md §5 least-privilege
        default) -- no elevated role, no project-scoped membership."""
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)

        _login(client, tenant, admin)
        response = client.post(
            "/api/v1/users/",
            data=json.dumps({"email": "freshuser@example.test", "name": "Fresh User"}),
            content_type="application/json",
        )
        assert response.status_code == 201, response.content
        new_user_id = response.json()["id"]

        with tenant_context(tenant.id):
            memberships = list(Membership.all_objects.filter(user_id=new_user_id))
        assert len(memberships) == 1
        assert memberships[0].project_id is None
        assert memberships[0].role.key == "member"

    def test_audit_entry_never_contains_password(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)

        _login(client, tenant, admin)
        response = client.post(
            "/api/v1/users/",
            data=json.dumps({"email": "audited@example.test", "name": "Audited"}),
            content_type="application/json",
        )
        assert response.status_code == 201, response.content
        password = response.json()["password"]
        new_user_id = response.json()["id"]

        entries = AuditLog.all_objects.filter(
            tenant_id=tenant.id, entity_type="user", entity_id=str(new_user_id)
        )
        assert entries.count() == 1
        entry = entries.first()
        assert entry.action == "user.manage"
        assert entry.actor_id == admin.id
        assert entry.before is None
        assert entry.after == {
            "id": new_user_id,
            "email": "audited@example.test",
            "name": "Audited",
        }
        # The password must not appear anywhere in the audit payload.
        assert "password" not in entry.after
        serialized = json.dumps(entry.after)
        assert password not in serialized

        # Never in the entry's `before` either (always None here, but assert
        # explicitly so a future change can't quietly add it there).
        assert entry.before is None


class TestUserList:
    def test_admin_sees_all_tenant_users(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        other = UserFactory(tenant=tenant, email="listed@example.test")

        _login(client, tenant, admin)
        response = client.get("/api/v1/users/")
        assert response.status_code == 200, response.content
        emails = {row["email"] for row in response.json()["results"]}
        assert admin.email in emails
        assert other.email in emails

    def test_project_lead_with_scoped_user_manage_can_list(self, client):
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, project, ROLE_PROJECT_LEAD)
        UserFactory(tenant=tenant, email="discoverable@example.test")

        _login(client, tenant, lead)
        response = client.get("/api/v1/users/")
        assert response.status_code == 200, response.content
        emails = {row["email"] for row in response.json()["results"]}
        assert "discoverable@example.test" in emails

    def test_project_lead_never_sees_another_tenants_users(self, client):
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, project, ROLE_PROJECT_LEAD)

        other_tenant = TenantFactory()
        UserFactory(tenant=other_tenant, email="secret@example.test")

        _login(client, tenant, lead)
        response = client.get("/api/v1/users/")
        assert response.status_code == 200, response.content
        emails = {row["email"] for row in response.json()["results"]}
        assert "secret@example.test" not in emails

    def test_member_with_no_user_manage_grant_is_denied(self, client):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)  # default: tenant-wide Member

        _login(client, tenant, member)
        response = client.get("/api/v1/users/")
        assert response.status_code == 403

    def test_viewer_with_no_user_manage_grant_is_denied(self, client):
        tenant = TenantFactory()
        viewer = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(viewer, ROLE_VIEWER)

        _login(client, tenant, viewer)
        response = client.get("/api/v1/users/")
        assert response.status_code == 403

    def test_search_filters_by_email_or_name(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        UserFactory(tenant=tenant, email="findme@example.test", name="Findable Person")
        UserFactory(tenant=tenant, email="other@example.test", name="Someone Else")

        _login(client, tenant, admin)
        response = client.get("/api/v1/users/", {"search": "findme"})
        assert response.status_code == 200, response.content
        emails = {row["email"] for row in response.json()["results"]}
        assert emails == {"findme@example.test"}

        response = client.get("/api/v1/users/", {"search": "Findable"})
        assert response.status_code == 200, response.content
        emails = {row["email"] for row in response.json()["results"]}
        assert emails == {"findme@example.test"}

    def test_cross_tenant_search_never_returns_another_tenants_users(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)

        other_tenant = TenantFactory()
        UserFactory(tenant=other_tenant, email="brute-force-target@example.test")

        _login(client, tenant, admin)
        response = client.get("/api/v1/users/", {"search": "brute-force-target"})
        assert response.status_code == 200, response.content
        assert response.json()["results"] == []
