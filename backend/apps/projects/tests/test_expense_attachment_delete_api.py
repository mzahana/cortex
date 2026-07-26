"""Acceptance test for `DELETE /api/v1/expense-attachments/{id}`
(`ExpenseAttachmentViewSet` in `apps.projects.api`, `ExpenseAttachmentPermission`
in `apps.projects.permissions`): the new best-effort storage purge + audit
path, mirroring `ProjectDocumentViewSet`'s delete tests
(`test_project_hub_uploads.py`/`test_project_hub_audit.py`/
`test_project_hub_rbac_matrix.py`/`test_project_hub_tenant_isolation.py`)
one-for-one for the `ExpenseAttachment` peer resource.

Five behaviors under test:
1. Happy path: `expense.manage` scoped to the attachment's own project ->
   204, DB row gone, file actually removed from storage.
2. Storage-delete is best-effort: file already missing (out-of-band) or
   `default_storage.delete` raising -> still 204 + DB row removed, never a
   500 (the try/except + warning log in `perform_destroy`).
3. RBAC: caller without `expense.manage` scoped to that project -> 403,
   attachment NOT deleted.
4. Tenant isolation: cross-tenant guessed id -> 404 (never 403/200), same
   convention as `ProjectDocumentViewSet`
   (`test_project_hub_tenant_isolation.py::test_document_delete_404`).
5. Audit: `AuditLog` row with `entity_type="expense_attachment"`,
   action `expense.manage`, `before` populated, `after=None`.
"""

from __future__ import annotations

import io
from unittest.mock import patch

import pytest

from apps.audit.models import AuditLog
from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
    ExpenseFactory,
    ProjectFactory,
    TenantFactory,
    UserFactory,
    add_project_membership,
    upgrade_tenant_wide_role,
)
from apps.projects.models import Expense, ExpenseAttachment
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


def _upload_attachment(client, expense_id, filename="invoice.pdf", content=b"fake-invoice-bytes"):
    upload = io.BytesIO(content)
    upload.name = filename
    response = client.post(
        f"/api/v1/expenses/{expense_id}/attachment/",
        data={"file": upload, "kind": "doc"},
    )
    assert response.status_code == 201, response.content
    return response.json()


class TestExpenseAttachmentDeleteHappyPath:
    def test_admin_deletes_attachment_204_db_row_and_storage_file_gone(
        self, client, settings, tmp_path
    ):
        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant)
        expense = ExpenseFactory(tenant=tenant, project=project, amount="88.00")
        _login(client, tenant, admin)

        body = _upload_attachment(client, expense.id)
        attachment_id = body["id"]
        storage_key = body["storage_key"]

        from django.core.files.storage import default_storage

        assert default_storage.exists(storage_key)

        delete_resp = client.delete(f"/api/v1/expense-attachments/{attachment_id}/")
        assert delete_resp.status_code == 204, delete_resp.content

        with tenant_context(tenant.id):
            assert not ExpenseAttachment.objects.filter(pk=attachment_id).exists()
        assert not ExpenseAttachment.all_objects.filter(pk=attachment_id).exists()

        assert not default_storage.exists(storage_key)

    def test_project_lead_scoped_to_own_project_can_delete(self, client, settings, tmp_path):
        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, project, ROLE_PROJECT_LEAD)
        expense = ExpenseFactory(tenant=tenant, project=project, amount="42.00")
        _login(client, tenant, lead)

        body = _upload_attachment(client, expense.id)
        attachment_id = body["id"]

        delete_resp = client.delete(f"/api/v1/expense-attachments/{attachment_id}/")
        assert delete_resp.status_code == 204, delete_resp.content
        assert not ExpenseAttachment.all_objects.filter(pk=attachment_id).exists()


class TestExpenseAttachmentDeleteStorageBestEffort:
    def test_delete_succeeds_even_if_file_already_missing_from_storage(
        self, client, settings, tmp_path
    ):
        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant)
        expense = ExpenseFactory(tenant=tenant, project=project, amount="10.00")
        _login(client, tenant, admin)

        body = _upload_attachment(client, expense.id)
        attachment_id = body["id"]
        storage_key = body["storage_key"]

        from django.core.files.storage import default_storage

        # Delete the file out-of-band first, so the DB row now points at a
        # storage object that no longer exists.
        default_storage.delete(storage_key)
        assert not default_storage.exists(storage_key)

        delete_resp = client.delete(f"/api/v1/expense-attachments/{attachment_id}/")
        assert delete_resp.status_code == 204, delete_resp.content
        assert not ExpenseAttachment.all_objects.filter(pk=attachment_id).exists()

    def test_delete_succeeds_even_if_storage_delete_raises(self, client, settings, tmp_path):
        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant)
        expense = ExpenseFactory(tenant=tenant, project=project, amount="10.00")
        _login(client, tenant, admin)

        body = _upload_attachment(client, expense.id)
        attachment_id = body["id"]

        with patch(
            "apps.projects.api.default_storage.delete",
            side_effect=OSError("storage backend unavailable"),
        ):
            delete_resp = client.delete(f"/api/v1/expense-attachments/{attachment_id}/")

        assert delete_resp.status_code == 204, delete_resp.content
        assert not ExpenseAttachment.all_objects.filter(pk=attachment_id).exists()


class TestExpenseAttachmentDeleteRBAC:
    def test_lead_scoped_to_a_different_project_gets_403_and_row_survives(
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

        delete_resp = client.delete(f"/api/v1/expense-attachments/{attachment_a.id}/")
        assert delete_resp.status_code == 403, delete_resp.content
        assert ExpenseAttachment.all_objects.filter(pk=attachment_a.id).exists()

    def test_viewer_without_expense_manage_gets_403_and_row_survives(
        self, client, settings, tmp_path
    ):
        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        viewer = UserFactory(tenant=tenant)
        add_project_membership(viewer, project, ROLE_VIEWER)
        expense = Expense.all_objects.create(
            tenant=tenant, project=project, amount="15.00", date="2026-01-01"
        )
        attachment = ExpenseAttachment.all_objects.create(
            tenant=tenant,
            expense=expense,
            storage_key="expense-attachments/x/y/invoice.pdf",
            filename="invoice.pdf",
        )
        _login(client, tenant, viewer)

        delete_resp = client.delete(f"/api/v1/expense-attachments/{attachment.id}/")
        assert delete_resp.status_code == 403, delete_resp.content
        assert ExpenseAttachment.all_objects.filter(pk=attachment.id).exists()


class TestExpenseAttachmentDeleteTenantIsolation:
    def test_cross_tenant_guessed_id_returns_404_not_403(self, client, settings, tmp_path):
        settings.MEDIA_ROOT = str(tmp_path)
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        admin_a = UserFactory(tenant=tenant_a)
        upgrade_tenant_wide_role(admin_a, ROLE_ADMIN)
        project_b = ProjectFactory(tenant=tenant_b)
        expense_b = ExpenseFactory(tenant=tenant_b, project=project_b, amount="10.00")
        attachment_b = ExpenseAttachment.all_objects.create(
            tenant=tenant_b,
            expense=expense_b,
            storage_key="expense-attachments/x/y/z.pdf",
            filename="z.pdf",
        )
        _login(client, tenant_a, admin_a)

        delete_resp = client.delete(f"/api/v1/expense-attachments/{attachment_b.id}/")
        assert delete_resp.status_code == 404, delete_resp.content
        assert ExpenseAttachment.all_objects.filter(pk=attachment_b.id).exists()


class TestExpenseAttachmentDeleteAudit:
    def test_delete_writes_audit_row(self, client, settings, tmp_path):
        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant)
        expense = ExpenseFactory(tenant=tenant, project=project, amount="99.00")
        _login(client, tenant, admin)

        body = _upload_attachment(client, expense.id, filename="receipt.pdf")
        attachment_id = body["id"]
        storage_key = body["storage_key"]

        delete_resp = client.delete(f"/api/v1/expense-attachments/{attachment_id}/")
        assert delete_resp.status_code == 204, delete_resp.content

        # The earlier upload (`POST .../attachment`) also writes an
        # `expense.manage`/`expense_attachment` audit row (create, `after`
        # populated) -- disambiguate the delete row via `after__isnull=True`,
        # same convention as `test_project_hub_audit.py::
        # test_document_delete_writes_audit_row`.
        entry = AuditLog.all_objects.get(
            tenant=tenant,
            action="expense.manage",
            entity_type="expense_attachment",
            entity_id=str(attachment_id),
            after__isnull=True,
        )
        assert entry.before["storage_key"] == storage_key
        assert entry.before["filename"] == "receipt.pdf"
        assert entry.after is None
        assert entry.actor_id == admin.id
