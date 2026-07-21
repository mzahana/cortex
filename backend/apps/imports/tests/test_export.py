"""T6.1 — `GET /api/v1/exports/assets.csv`: honors the same filters as
`GET /api/v1/assets`, RBAC-scopes `asset.export` the same way the list does
(Admin/Member tenant-wide, ProjectLead scoped to their own project, Viewer
denied), and never leaks another tenant's or another scope's assets.
"""

from __future__ import annotations

import csv
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


def _rows(response) -> list[dict[str, str]]:
    body = b"".join(response.streaming_content).decode("utf-8")
    return list(csv.DictReader(io.StringIO(body)))


class TestExportFilters:
    def test_category_filter_matches_list_endpoint_semantics(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        wanted = CategoryFactory(tenant=tenant, name="Wanted")
        other = CategoryFactory(tenant=tenant, name="Other")
        with tenant_context(tenant.id):
            Asset.objects.create(tenant=tenant, category=wanted, name="Keep")
            Asset.objects.create(tenant=tenant, category=other, name="Drop")
        _login(client, tenant, admin)

        response = client.get(f"/api/v1/exports/assets.csv?category={wanted.id}")
        assert response.status_code == 200, response.content
        rows = _rows(response)
        assert [r["name"] for r in rows] == ["Keep"]

    def test_retired_assets_excluded_by_default_like_the_list(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = CategoryFactory(tenant=tenant, name="C")
        with tenant_context(tenant.id):
            Asset.objects.create(tenant=tenant, category=category, name="Active")
            Asset.objects.create(
                tenant=tenant, category=category, name="Retired", status=Asset.Status.RETIRED
            )
        _login(client, tenant, admin)

        response = client.get("/api/v1/exports/assets.csv")
        rows = _rows(response)
        assert [r["name"] for r in rows] == ["Active"]

        response_incl = client.get("/api/v1/exports/assets.csv?include_retired=true")
        rows_incl = _rows(response_incl)
        assert sorted(r["name"] for r in rows_incl) == ["Active", "Retired"]


class TestExportRBAC:
    def test_viewer_is_denied(self, client):
        tenant = TenantFactory()
        viewer = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(viewer, ROLE_VIEWER)
        _login(client, tenant, viewer)

        response = client.get("/api/v1/exports/assets.csv")
        assert response.status_code == 403

    def test_member_can_export_tenant_wide(self, client):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)  # default membership is ROLE_MEMBER
        category = CategoryFactory(tenant=tenant, name="C")
        with tenant_context(tenant.id):
            Asset.objects.create(tenant=tenant, category=category, name="A")
        _login(client, tenant, member)

        response = client.get("/api/v1/exports/assets.csv")
        assert response.status_code == 200
        assert [r["name"] for r in _rows(response)] == ["A"]

    def test_project_lead_scoped_to_own_project_only(self, client):
        tenant = TenantFactory()
        category = CategoryFactory(tenant=tenant, name="C")
        project = ProjectFactory(tenant=tenant)
        other_project = ProjectFactory(tenant=tenant)

        lead = UserFactory(tenant=tenant)
        Membership.all_objects.filter(user=lead, project__isnull=True).delete()
        add_project_membership(lead, project, ROLE_PROJECT_LEAD)

        with tenant_context(tenant.id):
            Asset.objects.create(tenant=tenant, category=category, project=project, name="Mine")
            Asset.objects.create(
                tenant=tenant, category=category, project=other_project, name="NotMine"
            )
            Asset.objects.create(tenant=tenant, category=category, name="Pool")

        _login(client, tenant, lead)
        response = client.get("/api/v1/exports/assets.csv")
        assert response.status_code == 200
        assert [r["name"] for r in _rows(response)] == ["Mine"]


class TestExportTenantIsolation:
    def test_never_leaks_another_tenants_assets(self, client):
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        admin_a = UserFactory(tenant=tenant_a)
        upgrade_tenant_wide_role(admin_a, ROLE_ADMIN)
        category_a = CategoryFactory(tenant=tenant_a, name="C")
        category_b = CategoryFactory(tenant=tenant_b, name="C")
        with tenant_context(tenant_a.id):
            Asset.objects.create(tenant=tenant_a, category=category_a, name="MineA")
        with tenant_context(tenant_b.id):
            Asset.objects.create(tenant=tenant_b, category=category_b, name="TheirsB")

        _login(client, tenant_a, admin_a)
        response = client.get("/api/v1/exports/assets.csv")
        rows = _rows(response)
        assert [r["name"] for r in rows] == ["MineA"]


class TestExportCustomFieldColumns:
    def test_custom_field_column_included_and_populated(self, client):
        from apps.common.tests.factories import CustomFieldDefFactory

        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = CategoryFactory(tenant=tenant, name="Compute")
        CustomFieldDefFactory(category=category, key="vram_gb", data_type="int")
        with tenant_context(tenant.id):
            from apps.assets.models import AssetFieldValue
            from apps.catalog.models import CustomFieldDef

            asset = Asset.objects.create(tenant=tenant, category=category, name="Box")
            field_def = CustomFieldDef.objects.get(category=category, key="vram_gb")
            AssetFieldValue.objects.create(
                tenant=tenant, asset=asset, field_def=field_def, value=32
            )
        _login(client, tenant, admin)

        response = client.get("/api/v1/exports/assets.csv")
        rows = _rows(response)
        assert rows[0]["vram_gb"] == "32"
