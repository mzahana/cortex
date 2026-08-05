"""`POST /api/v1/projects/{id}/archive` — the structured ZIP bundle of a
project's ORIGINAL documents/attachments (`apps.projects.archive`).

Proves the properties an auditor's bundle actually has to have: a predictable
folder structure, the original bytes (not a rasterized render), a manifest
covering every member, the same `expense.view`-scoped-to-this-project gate the
report PDF has, and a size cap that fails the job cleanly instead of
exhausting the worker.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
    AssetFactory,
    CategoryFactory,
    ExpenseFactory,
    ProjectFactory,
    TenantFactory,
    UserFactory,
    add_project_membership,
    upgrade_tenant_wide_role,
)
from apps.jobs.models import Job
from apps.projects.archive import build_project_archive
from apps.projects.models import ExpenseAttachment, ProjectDocument
from apps.rbac.permission_keys import ROLE_ADMIN, ROLE_MEMBER, ROLE_PROJECT_LEAD
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


def _store(key: str, content: bytes) -> str:
    return default_storage.save(key, ContentFile(content))


def _project_document(
    project, *, kind="contract", filename="contract.pdf", content=b"CONTRACT", storage_name=None
):
    """`storage_name` exists for the Zip-Slip case: `default_storage` itself
    refuses to *write* a traversal path, but `ProjectDocument.filename` is
    ordinary user-supplied metadata that CAN hold one — which is exactly the
    input the archive's own sanitizer has to defend against."""
    return ProjectDocument.all_objects.create(
        tenant=project.tenant,
        project=project,
        kind=kind,
        storage_key=_store(f"test-docs/{storage_name or filename}", content),
        filename=filename,
        content_type="application/pdf",
        size=len(content),
    )


def _invoice(expense, *, filename="invoice-42.pdf", content=b"INVOICE"):
    return ExpenseAttachment.all_objects.create(
        tenant=expense.tenant,
        expense=expense,
        storage_key=_store(f"test-invoices/{filename}", content),
        filename=filename,
        content_type="application/pdf",
        size=len(content),
    )


def _names(zip_bytes: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        return zf.namelist()


class TestArchiveStructure:
    def test_bundle_has_the_documented_folder_layout(self):
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant, name="Grant One", code="G-001")
        _project_document(project, kind="proposal", filename="proposal.pdf")
        expense = ExpenseFactory(tenant=tenant, project=project, invoice_number="INV-7")
        _invoice(expense)

        with tenant_context(tenant.id):
            archive_file, stats = build_project_archive(
                project=project, generated_by="qa@example.com"
            )
        try:
            names = _names(archive_file.read())
        finally:
            archive_file.close()

        assert "G-001/README.txt" in names
        assert "G-001/manifest.csv" in names
        assert "G-001/expenses.csv" in names
        assert any(n.startswith("G-001/documents/proposal/") for n in names)
        assert any(n.startswith("G-001/invoices/") for n in names)
        assert stats.file_count == 2

    def test_original_bytes_are_preserved(self):
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant, code="G-002")
        _project_document(project, filename="contract.pdf", content=b"EXACT-ORIGINAL-BYTES")

        with tenant_context(tenant.id):
            archive_file, _ = build_project_archive(project=project)
        try:
            with zipfile.ZipFile(io.BytesIO(archive_file.read())) as zf:
                member = next(n for n in zf.namelist() if "documents/" in n)
                assert zf.read(member) == b"EXACT-ORIGINAL-BYTES"
        finally:
            archive_file.close()

    def test_manifest_lists_every_file(self):
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant, code="G-003")
        _project_document(project, filename="a.pdf")
        expense = ExpenseFactory(tenant=tenant, project=project)
        _invoice(expense, filename="b.pdf")

        with tenant_context(tenant.id):
            archive_file, _ = build_project_archive(project=project)
        try:
            with zipfile.ZipFile(io.BytesIO(archive_file.read())) as zf:
                manifest = zf.read("G-003/manifest.csv").decode()
        finally:
            archive_file.close()

        assert manifest.count("\n") >= 3  # header + 2 rows
        assert "a.pdf" in manifest and "b.pdf" in manifest

    def test_missing_storage_file_is_recorded_not_fatal(self):
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant, code="G-004")
        document = _project_document(project, filename="gone.pdf")
        default_storage.delete(document.storage_key)

        with tenant_context(tenant.id):
            archive_file, stats = build_project_archive(project=project)
        try:
            with zipfile.ZipFile(io.BytesIO(archive_file.read())) as zf:
                manifest = zf.read("G-004/manifest.csv").decode()
        finally:
            archive_file.close()

        assert stats.missing_count == 1
        assert "MISSING" in manifest

    def test_hostile_filename_cannot_escape_the_folder(self):
        """Zip-Slip: `filename` is user-supplied upload metadata."""
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant, code="G-005")
        _project_document(project, filename="../../../../etc/passwd", storage_name="hostile.pdf")

        with tenant_context(tenant.id):
            archive_file, _ = build_project_archive(project=project)
        try:
            names = _names(archive_file.read())
        finally:
            archive_file.close()

        assert all(".." not in name for name in names)
        assert all(name.startswith("G-005/") for name in names)

    def test_asset_attachments_are_opt_in(self):
        tenant = TenantFactory()
        category = CategoryFactory(tenant=tenant)
        project = ProjectFactory(tenant=tenant, code="G-006")
        asset = AssetFactory(tenant=tenant, category=category, project=project)
        from apps.assets.models import Attachment

        Attachment.all_objects.create(
            tenant=tenant,
            asset=asset,
            kind="doc",
            storage_key=_store("test-assets/po.pdf", b"PO"),
            filename="po.pdf",
            content_type="application/pdf",
            size=2,
        )

        with tenant_context(tenant.id):
            off_file, _ = build_project_archive(project=project)
        try:
            off_names = _names(off_file.read())
        finally:
            off_file.close()
        with tenant_context(tenant.id):
            on_file, _ = build_project_archive(project=project, include_asset_attachments=True)
        try:
            on_names = _names(on_file.read())
        finally:
            on_file.close()

        assert not any("/assets/" in n for n in off_names)
        assert any("/assets/" in n for n in on_names)


class TestArchiveEndpoint:
    def test_admin_enqueues_a_job_that_succeeds(self, client, django_capture_on_commit_callbacks):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant, code="G-010")
        _project_document(project)
        _login(client, tenant, admin)

        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(f"/api/v1/projects/{project.id}/archive/")

        assert response.status_code == 202, response.content
        job = Job.all_objects.get(pk=response.json()["id"])
        # CELERY_TASK_ALWAYS_EAGER in the test settings runs it inline.
        assert job.status == Job.Status.SUCCEEDED, job.error
        assert job.result_filename.endswith(".zip")
        assert job.job_type == "project_archive_zip"

    def test_project_lead_can_archive_their_own_project(self, client):
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant, code="G-011")
        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, project, ROLE_PROJECT_LEAD)
        _login(client, tenant, lead)

        response = client.post(f"/api/v1/projects/{project.id}/archive/")

        assert response.status_code == 202, response.content

    def test_lead_of_another_project_is_denied(self, client):
        tenant = TenantFactory()
        mine = ProjectFactory(tenant=tenant, code="G-012")
        theirs = ProjectFactory(tenant=tenant, code="G-013")
        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, mine, ROLE_PROJECT_LEAD)
        _login(client, tenant, lead)

        response = client.post(f"/api/v1/projects/{theirs.id}/archive/")

        assert response.status_code in (403, 404), response.content

    def test_plain_member_is_denied(self, client):
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant, code="G-014")
        member = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(member, ROLE_MEMBER)
        _login(client, tenant, member)

        response = client.post(f"/api/v1/projects/{project.id}/archive/")

        assert response.status_code == 403, response.content

    def test_cross_tenant_project_id_is_not_reachable(self, client):
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        admin_a = UserFactory(tenant=tenant_a)
        upgrade_tenant_wide_role(admin_a, ROLE_ADMIN)
        foreign = ProjectFactory(tenant=tenant_b, code="G-015")
        _login(client, tenant_a, admin_a)

        response = client.post(f"/api/v1/projects/{foreign.id}/archive/")

        assert response.status_code == 404, response.content

    def test_the_export_is_audited(self, client):
        from apps.audit.models import AuditLog

        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant, code="G-016")
        _login(client, tenant, admin)

        client.post(f"/api/v1/projects/{project.id}/archive/")

        assert AuditLog.all_objects.filter(
            tenant=tenant, entity_type="project_archive", entity_id=project.id
        ).exists()


class TestSizeCap:
    def test_over_the_cap_fails_the_job_with_an_actionable_message(
        self, client, monkeypatch, django_capture_on_commit_callbacks
    ):
        import apps.projects.archive as archive_module

        monkeypatch.setattr(archive_module, "MAX_ARCHIVE_BYTES", 8)

        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant, code="G-020")
        _project_document(project, content=b"x" * 1024)
        _login(client, tenant, admin)

        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(f"/api/v1/projects/{project.id}/archive/")

        job = Job.all_objects.get(pk=response.json()["id"])
        assert job.status == Job.Status.FAILED
        assert "archive limit" in job.error
