"""T6.1 — `import.run` RBAC (Admin only, tenant-wide, no ProjectLead scope
per `docs/rbac.md` §3) and tenant isolation (an import in tenant A never
matches/creates against tenant B's categories/locations/projects, and never
touches another tenant's `ImportJob` rows).
"""

from __future__ import annotations

import io

import pytest

from apps.assets.models import Asset
from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
    CategoryFactory,
    ProjectFactory,
    TenantFactory,
    UserFactory,
    add_project_membership,
    upgrade_tenant_wide_role,
)
from apps.imports.models import ImportJob
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


def _upload(client, csv_text: str = "name,category\nX,Y\n"):
    upload = io.BytesIO(csv_text.encode("utf-8"))
    upload.name = "assets.csv"
    return client.post("/api/v1/imports", data={"file": upload})


class TestImportRunRBAC:
    def test_admin_can_upload(self, client, django_capture_on_commit_callbacks):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        CategoryFactory(tenant=tenant, name="Y")
        _login(client, tenant, admin)

        with django_capture_on_commit_callbacks(execute=True):
            response = _upload(client)
        assert response.status_code == 202, response.content

    def test_member_is_denied(self, client):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)  # default membership is ROLE_MEMBER
        _login(client, tenant, member)

        response = _upload(client)
        assert response.status_code == 403

    def test_viewer_is_denied(self, client):
        tenant = TenantFactory()
        viewer = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(viewer, ROLE_VIEWER)
        _login(client, tenant, viewer)

        response = _upload(client)
        assert response.status_code == 403

    def test_project_lead_has_no_scoped_grant_and_is_denied(self, client):
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        lead = UserFactory(tenant=tenant)
        # Pure ProjectLead: only a project-scoped membership.
        Membership.all_objects.filter(user=lead, project__isnull=True).delete()
        add_project_membership(lead, project, ROLE_PROJECT_LEAD)
        _login(client, tenant, lead)

        response = _upload(client)
        assert response.status_code == 403

    def test_commit_and_detail_also_require_import_run(self, client):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        _login(client, tenant, member)

        with tenant_context(tenant.id):
            import_job = ImportJob.objects.create(
                tenant=tenant,
                source_filename="assets.csv",
                source_storage_key="imports/x.csv",
            )

        assert client.get(f"/api/v1/imports/{import_job.id}").status_code == 403
        assert (
            client.post(
                f"/api/v1/imports/{import_job.id}/commit",
                data="{}",
                content_type="application/json",
            ).status_code
            == 403
        )


class TestImportTenantIsolation:
    def test_category_names_never_resolve_across_tenants(
        self, client, django_capture_on_commit_callbacks
    ):
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        admin_a = UserFactory(tenant=tenant_a)
        upgrade_tenant_wide_role(admin_a, ROLE_ADMIN)
        # Same category NAME exists in tenant B only, not tenant A.
        CategoryFactory(tenant=tenant_b, name="OnlyInTenantB")
        _login(client, tenant_a, admin_a)

        csv_text = "name,category\nThing,OnlyInTenantB\n"
        with django_capture_on_commit_callbacks(execute=True):
            response = _upload(client, csv_text)
        import_id = response.json()["id"]

        detail = client.get(f"/api/v1/imports/{import_id}").json()
        assert detail["report"]["invalid_count"] == 1
        assert "category" in detail["report"]["rows"][0]["errors"]

        with tenant_context(tenant_a.id):
            assert Asset.objects.filter(name="Thing").count() == 0
        with tenant_context(tenant_b.id):
            assert Asset.objects.filter(name="Thing").count() == 0

    def test_import_job_detail_never_visible_across_tenants(self, client):
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        admin_a = UserFactory(tenant=tenant_a)
        upgrade_tenant_wide_role(admin_a, ROLE_ADMIN)
        admin_b = UserFactory(tenant=tenant_b)
        upgrade_tenant_wide_role(admin_b, ROLE_ADMIN)

        with tenant_context(tenant_b.id):
            import_job_b = ImportJob.objects.create(
                tenant=tenant_b,
                source_filename="assets.csv",
                source_storage_key="imports/x.csv",
                created_by=admin_b,
            )

        _login(client, tenant_a, admin_a)
        response = client.get(f"/api/v1/imports/{import_job_b.id}")
        assert response.status_code == 404
