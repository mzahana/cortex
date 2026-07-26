"""M7 acceptance test (`docs/tasks/M7-project-grants.md` "export.csv"):
streamed, field-selectable expense export contains exactly THIS project's
rows -- never another project's, even within the same tenant. RBAC-scoping
(lead of A can't export B) is proven in
`apps.projects.tests.test_project_hub_rbac_matrix::TestExportCsvIsPerProjectScoped`;
this module is the content-correctness half of the same acceptance line.
"""

from __future__ import annotations

import csv
import io

import pytest

from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
    ExpenseCategoryFactory,
    ExpenseFactory,
    ProjectFactory,
    TenantFactory,
    UserFactory,
    upgrade_tenant_wide_role,
)
from apps.rbac.permission_keys import ROLE_ADMIN

pytestmark = pytest.mark.django_db


def _login(client, tenant, user):
    response = client.post(
        "/api/v1/auth/login",
        {"tenant": tenant.slug, "email": user.email, "password": DEFAULT_TEST_PASSWORD},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return response


def _rows(response):
    body = b"".join(response.streaming_content).decode("utf-8")
    return list(csv.reader(io.StringIO(body)))


class TestExportCsvContent:
    def test_export_contains_this_projects_expenses_and_excludes_other_projects(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project_a = ProjectFactory(tenant=tenant, name="Project A")
        project_b = ProjectFactory(tenant=tenant, name="Project B")
        category = ExpenseCategoryFactory(tenant=tenant, name="Test Export Category")
        ExpenseFactory(
            tenant=tenant,
            project=project_a,
            category=category,
            amount="123.45",
            vendor="Acme Supplies",
            invoice_number="INV-001",
        )
        ExpenseFactory(
            tenant=tenant,
            project=project_b,
            category=category,
            amount="999.99",
            vendor="Other Project Vendor",
            invoice_number="INV-999",
        )
        _login(client, tenant, admin)

        response = client.get(f"/api/v1/projects/{project_a.id}/export.csv/")
        assert response.status_code == 200
        rows = _rows(response)
        header, *data_rows = rows
        assert header == [
            "date",
            "category",
            "vendor",
            "invoice_number",
            "amount",
            "currency",
            "description",
            "asset",
        ]
        assert len(data_rows) == 1
        assert data_rows[0][header.index("vendor")] == "Acme Supplies"
        assert data_rows[0][header.index("amount")] == "123.45"
        assert data_rows[0][header.index("category")] == "Test Export Category"

        body_text = b"".join(response.streaming_content).decode("utf-8")
        assert "Other Project Vendor" not in body_text
        assert "INV-999" not in body_text

    def test_export_field_selection_only_includes_requested_columns(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant)
        ExpenseFactory(tenant=tenant, project=project, amount="55.00", vendor="Field Test Vendor")
        _login(client, tenant, admin)

        response = client.get(f"/api/v1/projects/{project.id}/export.csv/?fields=amount,vendor")
        assert response.status_code == 200
        rows = _rows(response)
        header, *data_rows = rows
        assert header == ["amount", "vendor"]
        assert data_rows[0] == ["55.00", "Field Test Vendor"]

    def test_export_with_no_expenses_streams_just_the_header(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant)
        _login(client, tenant, admin)

        response = client.get(f"/api/v1/projects/{project.id}/export.csv/")
        assert response.status_code == 200
        rows = _rows(response)
        assert len(rows) == 1  # header only, no data rows
