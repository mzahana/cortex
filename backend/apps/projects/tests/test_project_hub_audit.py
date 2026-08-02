"""M7 acceptance test (`docs/tasks/M7-project-grants.md`: "Every mutating
action is audited"): the mutating actions the smoke suite
(`apps.projects.tests.test_project_hub_api::TestExpenseAuditTrail`) doesn't
reach — project budget PATCH, expense delete, document create/delete, and
invoice attachment upload. Each asserts an immutable before/after `AuditLog`
row with the correct actor (DB-level immutability itself — no raw
UPDATE/DELETE possible — is proven generically for every `AuditLog` row by
`apps.audit.tests.test_audit_db_immutability`, not re-proven per-action
here).
"""

from __future__ import annotations

import io
import json

import pytest

from apps.audit.models import AuditLog
from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
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


class TestProjectBudgetPatchAudit:
    def test_budget_patch_writes_before_after_audit_row_with_actor(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant, budget_total="1000.00")
        _login(client, tenant, admin)

        resp = client.patch(
            f"/api/v1/projects/{project.id}/",
            data=json.dumps({"budget_total": "1500.00"}),
            content_type="application/json",
        )
        assert resp.status_code == 200, resp.content

        entry = AuditLog.all_objects.get(
            tenant=tenant,
            action="project.manage",
            entity_type="project",
            entity_id=str(project.id),
        )
        assert entry.before is not None
        assert entry.before["budget_total"] == "1000.00"
        assert entry.after is not None
        assert entry.after["budget_total"] == "1500.00"
        assert entry.actor_id == admin.id

    def test_no_op_patch_writes_no_audit_row(self, client):
        """`ProjectViewSet.perform_update` only writes an entry `if before !=
        after` -- a PATCH that changes nothing must not spam the audit log."""
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant, budget_total="1000.00", name="Same Name")
        _login(client, tenant, admin)

        before_count = AuditLog.all_objects.filter(
            tenant=tenant, action="project.manage", entity_type="project", entity_id=str(project.id)
        ).count()
        resp = client.patch(
            f"/api/v1/projects/{project.id}/",
            data=json.dumps({"name": "Same Name"}),
            content_type="application/json",
        )
        assert resp.status_code == 200, resp.content
        after_count = AuditLog.all_objects.filter(
            tenant=tenant, action="project.manage", entity_type="project", entity_id=str(project.id)
        ).count()
        assert after_count == before_count


class TestExpenseDeleteAudit:
    def test_expense_delete_writes_before_after_null_audit_row(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant)
        expense = ExpenseFactory(tenant=tenant, project=project, amount="77.00")
        _login(client, tenant, admin)

        resp = client.delete(f"/api/v1/expenses/{expense.id}/")
        assert resp.status_code == 204, resp.content

        entry = AuditLog.all_objects.get(
            tenant=tenant,
            action="expense.manage",
            entity_type="expense",
            entity_id=str(expense.id),
        )
        assert entry.before is not None
        assert entry.before["amount"] == "77.00"
        assert entry.after is None
        assert entry.actor_id == admin.id


class TestDocumentAudit:
    def test_document_create_writes_audit_row(self, client, settings, tmp_path):
        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant)
        _login(client, tenant, admin)

        upload = io.BytesIO(b"%PDF-fake-bytes")
        upload.name = "proposal.pdf"
        resp = client.post(
            f"/api/v1/projects/{project.id}/documents/",
            data={"file": upload, "kind": "proposal"},
        )
        assert resp.status_code == 201, resp.content
        document_id = resp.json()["id"]

        entry = AuditLog.all_objects.get(
            tenant=tenant,
            action="project.manage",
            entity_type="project_document",
            entity_id=str(document_id),
        )
        assert entry.before is None
        assert entry.after is not None
        assert entry.after["filename"] == "proposal.pdf"
        assert entry.actor_id == admin.id

    def test_document_delete_writes_audit_row(self, client, settings, tmp_path):
        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant)
        _login(client, tenant, admin)

        upload = io.BytesIO(b"%PDF-fake-bytes")
        upload.name = "contract.pdf"
        create_resp = client.post(
            f"/api/v1/projects/{project.id}/documents/",
            data={"file": upload, "kind": "contract"},
        )
        document_id = create_resp.json()["id"]

        delete_resp = client.delete(f"/api/v1/documents/{document_id}/")
        assert delete_resp.status_code == 204, delete_resp.content

        entry = AuditLog.all_objects.get(
            tenant=tenant,
            action="project.manage",
            entity_type="project_document",
            entity_id=str(document_id),
            after__isnull=True,
        )
        assert entry.before is not None
        assert entry.before["filename"] == "contract.pdf"
        assert entry.after is None


class TestInvoiceAttachmentAudit:
    def test_attachment_upload_writes_audit_row(self, client, settings, tmp_path):
        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant)
        expense = ExpenseFactory(tenant=tenant, project=project, amount="15.00")
        _login(client, tenant, admin)

        upload = io.BytesIO(b"%PDF-fake-invoice")
        upload.name = "invoice.pdf"
        resp = client.post(
            f"/api/v1/expenses/{expense.id}/attachment/",
            data={"file": upload, "kind": "doc"},
        )
        assert resp.status_code == 201, resp.content
        attachment_id = resp.json()["id"]

        entry = AuditLog.all_objects.get(
            tenant=tenant,
            action="expense.manage",
            entity_type="expense_attachment",
            entity_id=str(attachment_id),
        )
        assert entry.before is None
        assert entry.after is not None
        assert entry.after["filename"] == "invoice.pdf"
        assert entry.actor_id == admin.id
