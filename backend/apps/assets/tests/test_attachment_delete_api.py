"""`DELETE /api/v1/attachments/{id}` — removing a photo/PO/receipt from an
asset (the missing other half of `POST /assets/{id}/attachments`).

Proves the two things that matter: the **file actually leaves storage** (not
just the DB row — an orphaned blob would consume the NAS volume forever with
nothing pointing at it), and the delete is scoped by `asset.attach` on the
OWNING ASSET's project, so a lead of project A cannot delete project B's
paperwork.
"""

from __future__ import annotations

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from apps.assets.models import Attachment
from apps.audit.models import AuditLog
from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
    AssetFactory,
    CategoryFactory,
    ProjectFactory,
    TenantFactory,
    UserFactory,
    add_project_membership,
    upgrade_tenant_wide_role,
)
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


def _attachment(asset, *, kind="doc", filename="po-1234.pdf", content=b"PURCHASE-ORDER"):
    return Attachment.all_objects.create(
        tenant=asset.tenant,
        asset=asset,
        kind=kind,
        storage_key=default_storage.save(f"test-attachments/{filename}", ContentFile(content)),
        filename=filename,
        content_type="application/pdf",
        size=len(content),
    )


class TestDeleteRemovesRowAndFile:
    def test_admin_delete_removes_the_db_row_and_the_stored_file(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        attachment = _attachment(asset)
        storage_key = attachment.storage_key
        assert default_storage.exists(storage_key)
        _login(client, tenant, admin)

        response = client.delete(f"/api/v1/attachments/{attachment.id}/")

        assert response.status_code == 204, response.content
        assert not Attachment.all_objects.filter(pk=attachment.id).exists()
        # The point of the feature: disk space is actually reclaimed.
        assert not default_storage.exists(storage_key)

    def test_photo_kind_deletes_the_same_way(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        attachment = _attachment(asset, kind="photo", filename="rig.jpg", content=b"JPEGDATA")
        storage_key = attachment.storage_key
        _login(client, tenant, admin)

        response = client.delete(f"/api/v1/attachments/{attachment.id}/")

        assert response.status_code == 204, response.content
        assert not default_storage.exists(storage_key)

    def test_already_missing_file_still_deletes_the_row(self, client):
        """Best-effort storage delete: a file that vanished out from under us
        must not block (or roll back) the DB delete."""
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        attachment = _attachment(asset)
        default_storage.delete(attachment.storage_key)
        _login(client, tenant, admin)

        response = client.delete(f"/api/v1/attachments/{attachment.id}/")

        assert response.status_code == 204, response.content
        assert not Attachment.all_objects.filter(pk=attachment.id).exists()

    def test_the_delete_is_audited(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        attachment = _attachment(asset)
        _login(client, tenant, admin)

        client.delete(f"/api/v1/attachments/{attachment.id}/")

        entry = AuditLog.all_objects.get(
            tenant=tenant, entity_type="attachment", entity_id=attachment.id
        )
        assert entry.after is None
        assert entry.before is not None
        assert entry.before["filename"] == "po-1234.pdf"

    def test_the_asset_itself_survives(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        attachment = _attachment(asset)
        _login(client, tenant, admin)

        client.delete(f"/api/v1/attachments/{attachment.id}/")

        detail = client.get(f"/api/v1/assets/{asset.id}/")
        assert detail.status_code == 200
        assert detail.json()["attachments"] == []


class TestDeleteRBAC:
    def test_lead_can_delete_within_their_own_project(self, client):
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, project, ROLE_PROJECT_LEAD)
        asset = AssetFactory(
            tenant=tenant, category=CategoryFactory(tenant=tenant), project=project
        )
        attachment = _attachment(asset)
        _login(client, tenant, lead)

        response = client.delete(f"/api/v1/attachments/{attachment.id}/")

        assert response.status_code == 204, response.content

    def test_lead_cannot_delete_another_projects_attachment(self, client):
        tenant = TenantFactory()
        mine = ProjectFactory(tenant=tenant)
        theirs = ProjectFactory(tenant=tenant)
        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, mine, ROLE_PROJECT_LEAD)
        # Drop the auto-created tenant-wide Member membership, which grants
        # `asset.attach` across the whole tenant and would mask this check.
        with tenant_context(tenant.id):
            lead.memberships.filter(project__isnull=True).delete()
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant), project=theirs)
        attachment = _attachment(asset)
        storage_key = attachment.storage_key
        _login(client, tenant, lead)

        response = client.delete(f"/api/v1/attachments/{attachment.id}/")

        assert response.status_code == 403, response.content
        assert Attachment.all_objects.filter(pk=attachment.id).exists()
        assert default_storage.exists(storage_key)

    def test_viewer_is_denied(self, client):
        tenant = TenantFactory()
        viewer = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(viewer, ROLE_VIEWER)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        attachment = _attachment(asset)
        _login(client, tenant, viewer)

        response = client.delete(f"/api/v1/attachments/{attachment.id}/")

        assert response.status_code == 403, response.content
        assert Attachment.all_objects.filter(pk=attachment.id).exists()

    def test_cross_tenant_attachment_id_is_a_404(self, client):
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        admin_a = UserFactory(tenant=tenant_a)
        upgrade_tenant_wide_role(admin_a, ROLE_ADMIN)
        foreign_asset = AssetFactory(tenant=tenant_b, category=CategoryFactory(tenant=tenant_b))
        foreign = _attachment(foreign_asset, filename="secret.pdf")
        _login(client, tenant_a, admin_a)

        response = client.delete(f"/api/v1/attachments/{foreign.id}/")

        assert response.status_code == 404, response.content
        assert Attachment.all_objects.filter(pk=foreign.id).exists()
        assert default_storage.exists(foreign.storage_key)

    def test_anonymous_is_denied(self, client):
        tenant = TenantFactory()
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        attachment = _attachment(asset)

        response = client.delete(f"/api/v1/attachments/{attachment.id}/")

        assert response.status_code in (401, 403)
        assert Attachment.all_objects.filter(pk=attachment.id).exists()
