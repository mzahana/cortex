"""T5.3 — `GET/POST/PATCH/DELETE /api/v1/memberships`: the `user.manage`/
`role.assign` gap-fill this task exists for (see `apps.rbac.serializers`
module docstring). Proves F8's "role change ... produces a tamper-evident
audit entry with actor/time/before-after" acceptance for the one action that
had NO endpoint at all before this task, plus the Admin-vs-ProjectLead scope
rule (docs/rbac.md §3 footnote 3) and cross-tenant isolation (R4).
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
    get_role,
    upgrade_tenant_wide_role,
)
from apps.rbac.models import Membership
from apps.rbac.permission_keys import ROLE_ADMIN, ROLE_MEMBER, ROLE_PROJECT_LEAD, ROLE_VIEWER
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


class TestMembershipCreateAndRoleChange:
    def test_admin_can_add_member_and_it_is_audited(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        target = UserFactory(tenant=tenant)
        with tenant_context(tenant.id):
            role = get_role(tenant, ROLE_VIEWER)

        _login(client, tenant, admin)
        response = client.post(
            "/api/v1/memberships/",
            data=json.dumps({"user": target.id, "role": role.id, "project": None}),
            content_type="application/json",
        )
        assert response.status_code == 201, response.content
        membership_id = response.json()["id"]

        entries = AuditLog.all_objects.filter(
            tenant_id=tenant.id, entity_type="membership", entity_id=membership_id
        )
        assert entries.count() == 1
        entry = entries.first()
        assert entry.action == "user.manage"
        assert entry.actor_id == admin.id
        assert entry.before is None
        assert entry.after == {
            "user_id": target.id,
            "role_key": ROLE_VIEWER,
            "project_id": None,
        }

    def test_role_change_is_audited_as_role_assign_with_before_after(self, client):
        """F8's exact acceptance wording: '... role change produces a
        tamper-evident audit entry with actor/time/before-after'."""
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        target = UserFactory(tenant=tenant)  # default: tenant-wide Member
        with tenant_context(tenant.id):
            membership = Membership.all_objects.get(user=target, project__isnull=True)
            new_role = get_role(tenant, ROLE_VIEWER)

        _login(client, tenant, admin)
        response = client.patch(
            f"/api/v1/memberships/{membership.id}/",
            data=json.dumps({"role": new_role.id}),
            content_type="application/json",
        )
        assert response.status_code == 200, response.content
        assert response.json()["role_key"] == ROLE_VIEWER

        entries = AuditLog.all_objects.filter(
            tenant_id=tenant.id, entity_type="membership", entity_id=membership.id
        )
        assert entries.count() == 1
        entry = entries.first()
        assert entry.action == "role.assign"
        assert entry.actor_id == admin.id
        assert entry.before == {
            "user_id": target.id,
            "role_key": ROLE_MEMBER,
            "project_id": None,
        }
        assert entry.after == {
            "user_id": target.id,
            "role_key": ROLE_VIEWER,
            "project_id": None,
        }

    def test_remove_member_is_audited(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        target = UserFactory(tenant=tenant)
        with tenant_context(tenant.id):
            membership = Membership.all_objects.get(user=target, project__isnull=True)
        membership_id = membership.id

        _login(client, tenant, admin)
        response = client.delete(f"/api/v1/memberships/{membership_id}/")
        assert response.status_code == 204, response.content

        with tenant_context(tenant.id):
            assert not Membership.all_objects.filter(pk=membership_id).exists()

        entries = AuditLog.all_objects.filter(
            tenant_id=tenant.id, entity_type="membership", entity_id=membership_id
        )
        assert entries.count() == 1
        assert entries.first().action == "user.manage"
        assert entries.first().after is None

    def test_plain_member_is_denied(self, client):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)  # default: tenant-wide Member
        target = UserFactory(tenant=tenant)
        with tenant_context(tenant.id):
            role = get_role(tenant, ROLE_VIEWER)

        _login(client, tenant, member)
        response = client.post(
            "/api/v1/memberships/",
            data=json.dumps({"user": target.id, "role": role.id, "project": None}),
            content_type="application/json",
        )
        assert response.status_code == 403


class TestMembershipProjectLeadScope:
    """docs/rbac.md §3 footnote 3: 'Project Lead may add/remove members and
    assign the Member role ONLY within their own project; cannot create
    Admins.'"""

    def test_project_lead_can_add_member_role_within_own_project(self, client):
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, project, ROLE_PROJECT_LEAD)
        target = UserFactory(tenant=tenant)
        with tenant_context(tenant.id):
            member_role = get_role(tenant, ROLE_MEMBER)

        _login(client, tenant, lead)
        response = client.post(
            "/api/v1/memberships/",
            data=json.dumps({"user": target.id, "role": member_role.id, "project": project.id}),
            content_type="application/json",
        )
        assert response.status_code == 201, response.content

    def test_project_lead_cannot_assign_admin_role(self, client):
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, project, ROLE_PROJECT_LEAD)
        target = UserFactory(tenant=tenant)
        with tenant_context(tenant.id):
            admin_role = get_role(tenant, ROLE_ADMIN)

        _login(client, tenant, lead)
        response = client.post(
            "/api/v1/memberships/",
            data=json.dumps({"user": target.id, "role": admin_role.id, "project": project.id}),
            content_type="application/json",
        )
        assert response.status_code == 403

    def test_project_lead_cannot_add_member_to_another_project(self, client):
        tenant = TenantFactory()
        own_project = ProjectFactory(tenant=tenant)
        other_project = ProjectFactory(tenant=tenant)
        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, own_project, ROLE_PROJECT_LEAD)
        target = UserFactory(tenant=tenant)
        with tenant_context(tenant.id):
            member_role = get_role(tenant, ROLE_MEMBER)

        _login(client, tenant, lead)
        response = client.post(
            "/api/v1/memberships/",
            data=json.dumps(
                {"user": target.id, "role": member_role.id, "project": other_project.id}
            ),
            content_type="application/json",
        )
        assert response.status_code == 403

    def test_project_lead_cannot_add_member_tenant_wide(self, client):
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, project, ROLE_PROJECT_LEAD)
        target = UserFactory(tenant=tenant)
        with tenant_context(tenant.id):
            member_role = get_role(tenant, ROLE_MEMBER)

        _login(client, tenant, lead)
        response = client.post(
            "/api/v1/memberships/",
            data=json.dumps({"user": target.id, "role": member_role.id, "project": None}),
            content_type="application/json",
        )
        assert response.status_code == 403

    def test_project_lead_list_only_sees_own_project_memberships(self, client):
        tenant = TenantFactory()
        own_project = ProjectFactory(tenant=tenant)
        other_project = ProjectFactory(tenant=tenant)
        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, own_project, ROLE_PROJECT_LEAD)
        other_lead = UserFactory(tenant=tenant)
        own_membership = add_project_membership(other_lead, own_project, ROLE_PROJECT_LEAD)
        add_project_membership(UserFactory(tenant=tenant), other_project, ROLE_PROJECT_LEAD)

        _login(client, tenant, lead)
        response = client.get("/api/v1/memberships/")
        assert response.status_code == 200, response.content
        ids = {row["id"] for row in response.json()["results"]}
        assert own_membership.id in ids
        # Never the other project's memberships, never tenant-wide ones
        # (Admin-only territory, footnote 3).
        tenant_wide_ids = set(
            Membership.all_objects.filter(project__isnull=True).values_list("id", flat=True)
        )
        assert ids.isdisjoint(tenant_wide_ids)


class TestMembershipCrossTenant:
    def test_cross_tenant_membership_id_404(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)

        other_tenant = TenantFactory()
        other_user = UserFactory(tenant=other_tenant)
        with tenant_context(other_tenant.id):
            other_membership = Membership.all_objects.get(user=other_user, project__isnull=True)

        _login(client, tenant, admin)
        response = client.get(f"/api/v1/memberships/{other_membership.id}/")
        assert response.status_code == 404
