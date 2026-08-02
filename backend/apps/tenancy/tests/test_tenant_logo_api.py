"""Acceptance tests for `GET/POST/DELETE /api/v1/tenancy/logo` — the lab logo
shown in the app chrome, uploaded from Admin -> Lab Branding.

Covers: upload happy path (+ the `/me` payload the UI actually reads), the
content-type/extension/size allowlist, RBAC (`tenant.manage` for writes, read
open to any member), tenant isolation (one lab's upload never touches
another's), removal, and the audit entries.
"""

from __future__ import annotations

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.audit.models import AuditLog
from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
    TenantFactory,
    UserFactory,
    upgrade_tenant_wide_role,
)
from apps.rbac.permission_keys import ROLE_ADMIN, ROLE_VIEWER
from apps.tenancy.context import tenant_context
from apps.tenancy.models import Tenant

pytestmark = pytest.mark.django_db

# 1x1 PNG (the smallest thing that is unambiguously a PNG).
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
    b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05"
    b"\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _png(name="logo.png"):
    return SimpleUploadedFile(name, PNG_BYTES, content_type="image/png")


def _login(client, tenant, user):
    response = client.post(
        "/api/v1/auth/login",
        {"tenant": tenant.slug, "email": user.email, "password": DEFAULT_TEST_PASSWORD},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return response


def _member(tenant, role_key):
    user = UserFactory(tenant=tenant)
    user.set_password(DEFAULT_TEST_PASSWORD)
    user.save()
    with tenant_context(tenant.id):
        upgrade_tenant_wide_role(user, role_key)
    return user


@pytest.fixture()
def tenant():
    return TenantFactory(name="Acme Robotics Lab")


@pytest.fixture()
def admin(tenant):
    return _member(tenant, ROLE_ADMIN)


@pytest.fixture(autouse=True)
def _media_root(settings, tmp_path):
    # Never write test uploads into the real media volume.
    settings.MEDIA_ROOT = str(tmp_path)


class TestTenantLogoUpload:
    def test_admin_uploads_logo_and_me_exposes_it(self, client, tenant, admin):
        _login(client, tenant, admin)

        response = client.post("/api/v1/tenancy/logo", {"file": _png()})
        assert response.status_code == 200, response.content
        body = response.json()
        assert body["name"] == "Acme Robotics Lab"
        assert body["logo_filename"] == "logo.png"
        assert body["logo_url"].startswith("/media/tenant-logos/")

        # The UI reads the logo off `/me`, not this endpoint — assert the
        # shape the chrome actually depends on.
        me = client.get("/api/v1/me").json()
        assert me["tenant"]["logo_url"] == body["logo_url"]
        assert me["tenant"]["name"] == "Acme Robotics Lab"

    def test_replacing_a_logo_deletes_the_previous_file(self, client, tenant, admin):
        from django.core.files.storage import default_storage

        _login(client, tenant, admin)
        first = client.post("/api/v1/tenancy/logo", {"file": _png("first.png")}).json()
        first_key = first["logo_url"].removeprefix("/media/")
        assert default_storage.exists(first_key)

        second = client.post("/api/v1/tenancy/logo", {"file": _png("second.png")}).json()
        assert second["logo_url"] != first["logo_url"]
        assert not default_storage.exists(first_key)

    def test_rejects_svg(self, client, tenant, admin):
        _login(client, tenant, admin)
        upload = SimpleUploadedFile("evil.svg", b"<svg/>", content_type="image/svg+xml")
        response = client.post("/api/v1/tenancy/logo", {"file": upload})
        assert response.status_code == 400
        assert "file" in response.json()["errors"]

    def test_rejects_extension_content_type_mismatch(self, client, tenant, admin):
        _login(client, tenant, admin)
        upload = SimpleUploadedFile("logo.html", PNG_BYTES, content_type="image/png")
        response = client.post("/api/v1/tenancy/logo", {"file": upload})
        assert response.status_code == 400

    def test_rejects_oversize_file(self, client, tenant, admin):
        from apps.tenancy.services import MAX_LOGO_UPLOAD_BYTES

        _login(client, tenant, admin)
        upload = SimpleUploadedFile(
            "big.png", b"x" * (MAX_LOGO_UPLOAD_BYTES + 1), content_type="image/png"
        )
        response = client.post("/api/v1/tenancy/logo", {"file": upload})
        assert response.status_code == 400

    def test_missing_file_is_a_400(self, client, tenant, admin):
        _login(client, tenant, admin)
        response = client.post("/api/v1/tenancy/logo", {})
        assert response.status_code == 400


class TestTenantLogoRbac:
    def test_viewer_can_read_but_not_write(self, client, tenant, admin):
        _login(client, tenant, admin)
        client.post("/api/v1/tenancy/logo", {"file": _png()})
        client.post("/api/v1/auth/logout")

        viewer = _member(tenant, ROLE_VIEWER)
        _login(client, tenant, viewer)

        assert client.get("/api/v1/tenancy/logo").status_code == 200
        assert client.post("/api/v1/tenancy/logo", {"file": _png()}).status_code == 403
        assert client.delete("/api/v1/tenancy/logo").status_code == 403

    def test_unauthenticated_is_rejected(self, client):
        assert client.get("/api/v1/tenancy/logo").status_code in (401, 403)


class TestTenantLogoIsolation:
    def test_upload_never_touches_another_tenant(self, client, tenant, admin):
        other = TenantFactory(name="Other Lab")

        _login(client, tenant, admin)
        client.post("/api/v1/tenancy/logo", {"file": _png()})

        assert Tenant.objects.get(pk=other.pk).logo_storage_key == ""
        assert Tenant.objects.get(pk=tenant.pk).logo_storage_key != ""


class TestTenantLogoDelete:
    def test_removes_logo_and_is_idempotent(self, client, tenant, admin):
        _login(client, tenant, admin)
        client.post("/api/v1/tenancy/logo", {"file": _png()})

        response = client.delete("/api/v1/tenancy/logo")
        assert response.status_code == 200, response.content
        assert response.json()["logo_url"] is None

        # Second delete: still 200, no logo, no second audit entry.
        assert client.delete("/api/v1/tenancy/logo").json()["logo_url"] is None
        with tenant_context(tenant.id):
            assert AuditLog.objects.filter(action="tenant.logo.delete").count() == 1


class TestTenantLogoAudit:
    def test_upload_and_delete_are_audited(self, client, tenant, admin):
        _login(client, tenant, admin)
        client.post("/api/v1/tenancy/logo", {"file": _png()})

        with tenant_context(tenant.id):
            entry = AuditLog.objects.filter(action="tenant.logo.update").latest("id")
        assert entry.entity_type == "tenant"
        assert entry.entity_id == str(tenant.id)  # AuditLog.entity_id is a CharField
        assert entry.actor_id == admin.id
        assert entry.before == {"logo_storage_key": "", "logo_filename": ""}
        assert entry.after["logo_filename"] == "logo.png"

        client.delete("/api/v1/tenancy/logo")
        with tenant_context(tenant.id):
            deleted = AuditLog.objects.filter(action="tenant.logo.delete").latest("id")
        assert deleted.after == {"logo_storage_key": "", "logo_filename": ""}
