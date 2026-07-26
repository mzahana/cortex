"""M7 acceptance test (`docs/tasks/M7-project-grants.md` exit criteria,
`docs/rbac.md` §3 additions matrix): the per-project 🟡 scope rule and the
Member/Viewer denial rows the smoke suite
(`apps.projects.tests.test_project_hub_api`) didn't reach — document
create/delete, invoice attachment upload/list, and export.csv, plus the
plain Member/Viewer financial-key denial on the top-level `/expenses/{id}`
resource.

Note on `documents` GET (deliberately NOT tested here as a 403): per
`apps.projects.permissions._action_permission_key`, `GET /projects/{id}/
documents` is gated by `project.view`, which the matrix grants Member
tenant-wide ("✅ own tenant, no financials unless granted") — so a
ProjectLead of project A legitimately gets 200 reading project B's document
LIST (metadata only: filename/kind/uploaded_by, no financial data) via that
tenant-wide grant, same as they can read project B's plain detail. That is
the documented design, not a leak (see `apps.projects.api.ProjectViewSet.
documents` and `permissions.py`'s own module docstring) — the write path
(POST/DELETE, gated by `project.manage`, never tenant-wide for a Lead) is
the actual per-project boundary this milestone promises for documents, and
is what's tested below.
"""

from __future__ import annotations

import io
import json

import pytest

from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
    ExpenseFactory,
    ProjectFactory,
    TenantFactory,
    UserFactory,
    add_project_membership,
    upgrade_tenant_wide_role,
)
from apps.projects.models import ProjectDocument
from apps.rbac.permission_keys import ROLE_ADMIN, ROLE_PROJECT_LEAD, ROLE_VIEWER

pytestmark = pytest.mark.django_db


def _login(client, tenant, user):
    response = client.post(
        "/api/v1/auth/login",
        {"tenant": tenant.slug, "email": user.email, "password": DEFAULT_TEST_PASSWORD},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return response


def _two_projects_one_lead(tenant):
    project_a = ProjectFactory(tenant=tenant, name="Project A", budget_total="1000.00")
    project_b = ProjectFactory(tenant=tenant, name="Project B", budget_total="2000.00")
    lead = UserFactory(tenant=tenant)
    add_project_membership(lead, project_a, ROLE_PROJECT_LEAD)
    return project_a, project_b, lead


class TestDocumentWriteIsPerProjectScoped:
    def test_lead_cannot_create_or_delete_other_projects_document(self, client, settings, tmp_path):
        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        project_a, project_b, lead = _two_projects_one_lead(tenant)
        document_b = ProjectDocument.all_objects.create(
            tenant=tenant,
            project=project_b,
            kind=ProjectDocument.Kind.CONTRACT,
            storage_key="project-documents/x/y/z.pdf",
            filename="z.pdf",
        )
        _login(client, tenant, lead)

        upload = io.BytesIO(b"%PDF-fake-bytes")
        upload.name = "contract.pdf"
        create_resp = client.post(
            f"/api/v1/projects/{project_b.id}/documents/",
            data={"file": upload, "kind": "contract"},
        )
        assert create_resp.status_code == 403, create_resp.content

        delete_resp = client.delete(f"/api/v1/documents/{document_b.id}/")
        assert delete_resp.status_code == 403, delete_resp.content

    def test_lead_can_create_and_delete_their_own_projects_document(
        self, client, settings, tmp_path
    ):
        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        project_a, _project_b, lead = _two_projects_one_lead(tenant)
        _login(client, tenant, lead)

        upload = io.BytesIO(b"%PDF-fake-bytes")
        upload.name = "proposal.pdf"
        create_resp = client.post(
            f"/api/v1/projects/{project_a.id}/documents/",
            data={"file": upload, "kind": "proposal"},
        )
        assert create_resp.status_code == 201, create_resp.content
        document_id = create_resp.json()["id"]

        delete_resp = client.delete(f"/api/v1/documents/{document_id}/")
        assert delete_resp.status_code == 204, delete_resp.content


class TestInvoiceAttachmentIsPerProjectScoped:
    def test_lead_cannot_upload_or_list_invoice_on_other_projects_expense(
        self, client, settings, tmp_path
    ):
        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        project_a, project_b, lead = _two_projects_one_lead(tenant)
        expense_b = ExpenseFactory(tenant=tenant, project=project_b, amount="30.00")
        _login(client, tenant, lead)

        list_resp = client.get(f"/api/v1/expenses/{expense_b.id}/attachment/")
        assert list_resp.status_code == 403, list_resp.content

        upload = io.BytesIO(b"%PDF-fake-invoice")
        upload.name = "invoice.pdf"
        upload_resp = client.post(
            f"/api/v1/expenses/{expense_b.id}/attachment/",
            data={"file": upload, "kind": "doc"},
        )
        assert upload_resp.status_code == 403, upload_resp.content

    def test_lead_can_upload_and_list_invoice_on_their_own_projects_expense(
        self, client, settings, tmp_path
    ):
        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        project_a, _project_b, lead = _two_projects_one_lead(tenant)
        expense_a = ExpenseFactory(tenant=tenant, project=project_a, amount="30.00")
        _login(client, tenant, lead)

        upload = io.BytesIO(b"%PDF-fake-invoice")
        upload.name = "invoice.pdf"
        upload_resp = client.post(
            f"/api/v1/expenses/{expense_a.id}/attachment/",
            data={"file": upload, "kind": "doc"},
        )
        assert upload_resp.status_code == 201, upload_resp.content

        list_resp = client.get(f"/api/v1/expenses/{expense_a.id}/attachment/")
        assert list_resp.status_code == 200, list_resp.content
        assert len(list_resp.json()) == 1


class TestExportCsvIsPerProjectScoped:
    def test_lead_cannot_export_other_projects_expenses(self, client):
        tenant = TenantFactory()
        project_a, project_b, lead = _two_projects_one_lead(tenant)
        ExpenseFactory(tenant=tenant, project=project_b, amount="99.00")
        _login(client, tenant, lead)

        resp = client.get(f"/api/v1/projects/{project_b.id}/export.csv/")
        assert resp.status_code == 403, getattr(resp, "content", resp)

    def test_lead_can_export_their_own_projects_expenses(self, client):
        tenant = TenantFactory()
        project_a, _project_b, lead = _two_projects_one_lead(tenant)
        ExpenseFactory(tenant=tenant, project=project_a, amount="12.34", vendor="Acme")
        _login(client, tenant, lead)

        resp = client.get(f"/api/v1/projects/{project_a.id}/export.csv/")
        assert resp.status_code == 200
        body = b"".join(resp.streaming_content).decode("utf-8")
        assert "Acme" in body
        assert "12.34" in body


class TestMemberAndViewerDeniedFinancialKeys:
    """Matrix rows: neither Member nor Viewer holds `expense.view`/
    `expense.manage` tenant-wide — both must 403 on the top-level
    `/expenses/{id}` resource, not just the nested list/create smoke already
    covers.
    """

    @pytest.mark.parametrize("role_key", [None, ROLE_VIEWER])
    def test_member_or_viewer_403s_on_expense_detail_update_delete(self, client, role_key):
        tenant = TenantFactory()
        user = UserFactory(tenant=tenant)
        if role_key is not None:
            upgrade_tenant_wide_role(user, role_key)
        project = ProjectFactory(tenant=tenant)
        expense = ExpenseFactory(tenant=tenant, project=project, amount="10.00")
        _login(client, tenant, user)

        assert client.get(f"/api/v1/expenses/{expense.id}/").status_code == 403
        assert (
            client.patch(
                f"/api/v1/expenses/{expense.id}/",
                data=json.dumps({"amount": "1.00"}),
                content_type="application/json",
            ).status_code
            == 403
        )
        assert client.delete(f"/api/v1/expenses/{expense.id}/").status_code == 403
        assert client.get(f"/api/v1/expenses/{expense.id}/attachment/").status_code == 403


class TestStructuralCrudStaysAdminOnly:
    """`create`/`destroy` on `/projects` are unchanged from the superseded
    catalog viewset: Admin-only (`tenant.manage`), never scoped to a
    ProjectLead even for a project they lead."""

    def test_project_lead_cannot_create_or_delete_projects(self, client):
        tenant = TenantFactory()
        project_a, _project_b, lead = _two_projects_one_lead(tenant)
        _login(client, tenant, lead)

        create_resp = client.post(
            "/api/v1/projects/",
            data=json.dumps({"name": "New Project"}),
            content_type="application/json",
        )
        assert create_resp.status_code == 403, create_resp.content

        delete_resp = client.delete(f"/api/v1/projects/{project_a.id}/")
        assert delete_resp.status_code == 403, delete_resp.content

    def test_admin_can_create_and_delete_projects(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        _login(client, tenant, admin)

        create_resp = client.post(
            "/api/v1/projects/",
            data=json.dumps({"name": "Admin Created Project"}),
            content_type="application/json",
        )
        assert create_resp.status_code == 201, create_resp.content

        delete_resp = client.delete(f"/api/v1/projects/{create_resp.json()['id']}/")
        assert delete_resp.status_code == 204, delete_resp.content
