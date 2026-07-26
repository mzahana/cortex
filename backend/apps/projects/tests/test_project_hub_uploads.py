"""M7 acceptance test (`docs/tasks/M7-project-grants.md` "Uploads"): invoice
attachments and project documents round-trip through the shared
`apps.assets.services.save_attachment_file` writer — bytes land on the
storage backend, only `storage_key` + metadata ever reach the DB, and the
row is retrievable by an authorized caller / denied to an unauthorized one.
Mirrors `apps.assets.tests.test_assets_api::TestAttachmentStorage` exactly,
adapted to the two new M7 anchors.
"""

from __future__ import annotations

import io

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
from apps.projects.models import Expense, ExpenseAttachment, ProjectDocument
from apps.rbac.permission_keys import ROLE_ADMIN, ROLE_PROJECT_LEAD
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


class TestInvoiceAttachmentRoundTrip:
    def test_invoice_upload_stores_only_key_bytes_are_on_disk_and_retrievable(
        self, client, settings, tmp_path
    ):
        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant)
        expense = ExpenseFactory(tenant=tenant, project=project, amount="88.00")
        _login(client, tenant, admin)

        upload = io.BytesIO(b"fake-invoice-pdf-bytes")
        upload.name = "invoice.pdf"
        response = client.post(
            f"/api/v1/expenses/{expense.id}/attachment/",
            data={"file": upload, "kind": "doc"},
        )
        assert response.status_code == 201, response.content
        body = response.json()
        storage_key = body["storage_key"]
        assert storage_key
        assert str(tenant.id) in storage_key
        assert str(expense.id) in storage_key

        with tenant_context(tenant.id):
            attachment = ExpenseAttachment.objects.get(pk=body["id"])
            assert attachment.storage_key == storage_key
            assert attachment.filename == "invoice.pdf"
            assert attachment.size == len(b"fake-invoice-pdf-bytes")

        on_disk = tmp_path / storage_key
        assert on_disk.exists()
        assert on_disk.read_bytes() == b"fake-invoice-pdf-bytes"

        # DB column is a short key/path, never the file's bytes.
        assert len(attachment.storage_key) < 500
        assert b"fake-invoice-pdf-bytes" not in attachment.storage_key.encode()

        # Retrievable by an authorized caller (same admin, same tenant).
        list_resp = client.get(f"/api/v1/expenses/{expense.id}/attachment/")
        assert list_resp.status_code == 200
        assert list_resp.json()[0]["storage_key"] == storage_key

    def test_invoice_attachment_403s_for_an_unauthorized_project_caller(
        self, client, settings, tmp_path
    ):
        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        project_a = ProjectFactory(tenant=tenant, name="Project A")
        project_b = ProjectFactory(tenant=tenant, name="Project B")
        lead_b = UserFactory(tenant=tenant)
        add_project_membership(lead_b, project_b, ROLE_PROJECT_LEAD)
        expense_a = Expense.all_objects.create(
            tenant=tenant, project=project_a, amount="40.00", date="2026-01-01"
        )
        attachment_a = ExpenseAttachment.all_objects.create(
            tenant=tenant,
            expense=expense_a,
            storage_key="expense-attachments/x/y/invoice.pdf",
            filename="invoice.pdf",
        )
        _login(client, tenant, lead_b)

        list_resp = client.get(f"/api/v1/expenses/{expense_a.id}/attachment/")
        assert list_resp.status_code == 403, list_resp.content
        # A Lead scoped to a DIFFERENT project also can't upload a new one.
        upload = io.BytesIO(b"fake-invoice-pdf-bytes")
        upload.name = "invoice2.pdf"
        upload_resp = client.post(
            f"/api/v1/expenses/{expense_a.id}/attachment/",
            data={"file": upload, "kind": "doc"},
        )
        assert upload_resp.status_code == 403, upload_resp.content
        assert attachment_a.id  # sanity: the pre-existing row is untouched


class TestProjectDocumentRoundTrip:
    @pytest.mark.parametrize("kind", ["proposal", "contract", "progress_report"])
    def test_document_upload_stores_only_key_bytes_are_on_disk_and_retrievable(
        self, client, settings, tmp_path, kind
    ):
        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant)
        _login(client, tenant, admin)

        upload = io.BytesIO(b"fake-document-bytes")
        upload.name = f"{kind}.pdf"
        response = client.post(
            f"/api/v1/projects/{project.id}/documents/",
            data={"file": upload, "kind": kind},
        )
        assert response.status_code == 201, response.content
        body = response.json()
        assert body["kind"] == kind
        storage_key = body["storage_key"]
        assert storage_key
        assert str(tenant.id) in storage_key
        assert str(project.id) in storage_key

        with tenant_context(tenant.id):
            document = ProjectDocument.objects.get(pk=body["id"])
            assert document.storage_key == storage_key
            assert document.size == len(b"fake-document-bytes")

        on_disk = tmp_path / storage_key
        assert on_disk.exists()
        assert on_disk.read_bytes() == b"fake-document-bytes"
        assert len(document.storage_key) < 500

        list_resp = client.get(f"/api/v1/projects/{project.id}/documents/")
        assert list_resp.status_code == 200
        assert any(d["storage_key"] == storage_key for d in list_resp.json()["results"])

    def test_document_upload_403s_for_an_unauthorized_project_caller(
        self, client, settings, tmp_path
    ):
        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        project_a = ProjectFactory(tenant=tenant, name="Project A")
        project_b = ProjectFactory(tenant=tenant, name="Project B")
        lead_b = UserFactory(tenant=tenant)
        add_project_membership(lead_b, project_b, ROLE_PROJECT_LEAD)
        _login(client, tenant, lead_b)

        upload = io.BytesIO(b"fake-document-bytes")
        upload.name = "proposal.pdf"
        response = client.post(
            f"/api/v1/projects/{project_a.id}/documents/",
            data={"file": upload, "kind": "proposal"},
        )
        assert response.status_code == 403, response.content
