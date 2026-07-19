"""T1.2 — Asset registry core: tenant scoping, scoped RBAC, custom-field
validation (F2), retire lifecycle, attachment storage (add-endpoint golden
path checklist).
"""

from __future__ import annotations

import io
import json

import pytest

from apps.assets.models import Asset, AssetFieldValue, Attachment, TagLink
from apps.audit.models import AuditLog
from apps.catalog.models import Category, CustomFieldDef
from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
    CategoryFactory,
    LocationFactory,
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


def _make_compute_category(tenant) -> Category:
    category = CategoryFactory(tenant=tenant, name="Compute", default_is_consumable=False)
    CustomFieldDef.all_objects.create(
        tenant=tenant,
        category=category,
        key="gpu_model",
        label="GPU Model",
        data_type=CustomFieldDef.DataType.TEXT,
        required=True,
        order=0,
    )
    CustomFieldDef.all_objects.create(
        tenant=tenant,
        category=category,
        key="vram_gb",
        label="VRAM (GB)",
        data_type=CustomFieldDef.DataType.INT,
        unit="GB",
        required=True,
        order=1,
    )
    CustomFieldDef.all_objects.create(
        tenant=tenant,
        category=category,
        key="connectivity",
        label="Connectivity",
        data_type=CustomFieldDef.DataType.ENUM,
        enum_options=["wifi", "ethernet"],
        required=False,
        order=2,
    )
    return category


def _make_component_category(tenant) -> Category:
    return CategoryFactory(tenant=tenant, name="Components", default_is_consumable=True)


class TestAssetCreateWithCustomFields:
    """Core F2: create a Compute asset with GPU/VRAM custom-field values;
    invalid enum/type values are cleanly rejected (400 RFC-7807)."""

    def test_create_compute_asset_with_valid_custom_fields(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = _make_compute_category(tenant)
        _login(client, tenant, admin)

        response = client.post(
            "/api/v1/assets/",
            data=json.dumps(
                {
                    "category": category.id,
                    "name": "GPU Server 1",
                    "custom_field_values": {
                        "gpu_model": "RTX 4090",
                        "vram_gb": 24,
                        "connectivity": "ethernet",
                    },
                    "tags": ["gpu", "compute"],
                }
            ),
            content_type="application/json",
        )
        assert response.status_code == 201, response.content
        body = response.json()
        assert body["field_values"] == {
            "gpu_model": "RTX 4090",
            "vram_gb": 24,
            "connectivity": "ethernet",
        }
        assert sorted(body["tags"]) == ["compute", "gpu"]
        assert body["is_consumable"] is False
        assert body["status"] == "available"
        assert body["qr_token"]

        with tenant_context(tenant.id):
            asset = Asset.objects.get(pk=body["id"])
            assert AssetFieldValue.objects.filter(asset=asset).count() == 3
            assert TagLink.objects.filter(asset=asset).count() == 2

    def test_missing_required_custom_field_is_400(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = _make_compute_category(tenant)
        _login(client, tenant, admin)

        response = client.post(
            "/api/v1/assets/",
            data=json.dumps(
                {
                    "category": category.id,
                    "name": "GPU Server 2",
                    "custom_field_values": {"gpu_model": "RTX 4090"},  # vram_gb missing
                }
            ),
            content_type="application/json",
        )
        assert response.status_code == 400, response.content
        body = response.json()
        assert body["status"] == 400
        assert "vram_gb" in body["errors"]["custom_field_values"]

    def test_bad_enum_value_is_400(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = _make_compute_category(tenant)
        _login(client, tenant, admin)

        response = client.post(
            "/api/v1/assets/",
            data=json.dumps(
                {
                    "category": category.id,
                    "name": "GPU Server 3",
                    "custom_field_values": {
                        "gpu_model": "RTX 4090",
                        "vram_gb": 24,
                        "connectivity": "bluetooth",  # not in enum_options
                    },
                }
            ),
            content_type="application/json",
        )
        assert response.status_code == 400, response.content
        assert "connectivity" in response.json()["errors"]["custom_field_values"]

    def test_bad_type_value_is_400(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = _make_compute_category(tenant)
        _login(client, tenant, admin)

        response = client.post(
            "/api/v1/assets/",
            data=json.dumps(
                {
                    "category": category.id,
                    "name": "GPU Server 4",
                    "custom_field_values": {
                        "gpu_model": "RTX 4090",
                        "vram_gb": "not-a-number",
                    },
                }
            ),
            content_type="application/json",
        )
        assert response.status_code == 400, response.content
        assert "vram_gb" in response.json()["errors"]["custom_field_values"]

    def test_unknown_custom_field_key_is_400(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = _make_compute_category(tenant)
        _login(client, tenant, admin)

        response = client.post(
            "/api/v1/assets/",
            data=json.dumps(
                {
                    "category": category.id,
                    "name": "GPU Server 5",
                    "custom_field_values": {
                        "gpu_model": "RTX 4090",
                        "vram_gb": 24,
                        "typo_field": "oops",
                    },
                }
            ),
            content_type="application/json",
        )
        assert response.status_code == 400, response.content
        assert "typo_field" in response.json()["errors"]["custom_field_values"]

    def test_create_consumable_component_defaults_is_consumable_from_category(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = _make_component_category(tenant)
        _login(client, tenant, admin)

        response = client.post(
            "/api/v1/assets/",
            data=json.dumps({"category": category.id, "name": "M3 Screws"}),
            content_type="application/json",
        )
        assert response.status_code == 201, response.content
        assert response.json()["is_consumable"] is True


class TestAssetRetireLifecycle:
    def test_retire_hides_from_default_list_but_retains_row(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = _make_component_category(tenant)
        _login(client, tenant, admin)

        create_resp = client.post(
            "/api/v1/assets/",
            data=json.dumps({"category": category.id, "name": "Old Drone"}),
            content_type="application/json",
        )
        asset_id = create_resp.json()["id"]

        retire_resp = client.post(f"/api/v1/assets/{asset_id}/retire/")
        assert retire_resp.status_code == 200, retire_resp.content
        assert retire_resp.json()["status"] == "retired"
        assert retire_resp.json()["retired_at"] is not None

        list_resp = client.get("/api/v1/assets/")
        assert asset_id not in [a["id"] for a in list_resp.json()["results"]]

        detail_resp = client.get(f"/api/v1/assets/{asset_id}/")
        assert detail_resp.status_code == 200
        assert detail_resp.json()["status"] == "retired"

        include_retired_resp = client.get("/api/v1/assets/?include_retired=true")
        assert asset_id in [a["id"] for a in include_retired_resp.json()["results"]]

        with tenant_context(tenant.id):
            assert Asset.objects.filter(pk=asset_id).exists()

    def test_retire_writes_audit_log_entry(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = _make_component_category(tenant)
        _login(client, tenant, admin)

        asset_id = client.post(
            "/api/v1/assets/",
            data=json.dumps({"category": category.id, "name": "Widget"}),
            content_type="application/json",
        ).json()["id"]

        client.post(f"/api/v1/assets/{asset_id}/retire/")

        entry = AuditLog.all_objects.get(
            tenant=tenant, entity_type="asset", entity_id=str(asset_id)
        )
        assert entry.action == "asset.retire"
        assert entry.before is not None and entry.before["status"] == "available"
        assert entry.after is not None and entry.after["status"] == "retired"
        assert entry.actor_id == admin.id

    def test_retire_is_idempotent_and_does_not_double_audit(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = _make_component_category(tenant)
        _login(client, tenant, admin)

        asset_id = client.post(
            "/api/v1/assets/",
            data=json.dumps({"category": category.id, "name": "Widget"}),
            content_type="application/json",
        ).json()["id"]

        first = client.post(f"/api/v1/assets/{asset_id}/retire/")
        second = client.post(f"/api/v1/assets/{asset_id}/retire/")
        assert first.status_code == 200
        assert second.status_code == 200

        assert (
            AuditLog.all_objects.filter(
                tenant=tenant, entity_type="asset", entity_id=str(asset_id)
            ).count()
            == 1
        )

    def test_cannot_retire_via_plain_patch(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = _make_component_category(tenant)
        _login(client, tenant, admin)

        asset_id = client.post(
            "/api/v1/assets/",
            data=json.dumps({"category": category.id, "name": "Widget"}),
            content_type="application/json",
        ).json()["id"]

        response = client.patch(
            f"/api/v1/assets/{asset_id}/",
            data=json.dumps({"status": "retired"}),
            content_type="application/json",
        )
        assert response.status_code == 400
        assert "status" in response.json()["errors"]


class TestAttachmentStorage:
    def test_attachment_upload_stores_only_key_bytes_on_volume(self, client, settings, tmp_path):
        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = _make_component_category(tenant)
        _login(client, tenant, admin)

        asset_id = client.post(
            "/api/v1/assets/",
            data=json.dumps({"category": category.id, "name": "Widget"}),
            content_type="application/json",
        ).json()["id"]

        upload = io.BytesIO(b"fake-jpeg-bytes")
        upload.name = "photo.jpg"
        response = client.post(
            f"/api/v1/assets/{asset_id}/attachments/",
            data={"file": upload, "kind": "photo"},
        )
        assert response.status_code == 201, response.content
        body = response.json()
        storage_key = body["storage_key"]
        assert storage_key
        assert str(tenant.id) in storage_key
        assert str(asset_id) in storage_key

        with tenant_context(tenant.id):
            attachment = Attachment.objects.get(pk=body["id"])
            assert attachment.storage_key == storage_key
            assert attachment.filename == "photo.jpg"
            assert attachment.size == len(b"fake-jpeg-bytes")

        on_disk = tmp_path / storage_key
        assert on_disk.exists()
        assert on_disk.read_bytes() == b"fake-jpeg-bytes"

        # The DB column is a short key/path string, never the file's bytes.
        assert len(attachment.storage_key) < 500
        assert b"fake-jpeg-bytes" not in attachment.storage_key.encode()


class TestAssetTenantIsolation:
    def test_list_never_shows_another_tenants_assets(self, client):
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        admin_a = UserFactory(tenant=tenant_a)
        upgrade_tenant_wide_role(admin_a, ROLE_ADMIN)
        category_a = _make_component_category(tenant_a)
        category_b = _make_component_category(tenant_b)

        admin_b = UserFactory(tenant=tenant_b)
        upgrade_tenant_wide_role(admin_b, ROLE_ADMIN)
        _login(client, tenant_b, admin_b)
        client.post(
            "/api/v1/assets/",
            data=json.dumps({"category": category_b.id, "name": "Tenant B Asset"}),
            content_type="application/json",
        )
        client.post("/api/v1/auth/logout")

        _login(client, tenant_a, admin_a)
        client.post(
            "/api/v1/assets/",
            data=json.dumps({"category": category_a.id, "name": "Tenant A Asset"}),
            content_type="application/json",
        )

        response = client.get("/api/v1/assets/")
        assert response.status_code == 200
        assert response.json()["count"] == 1
        assert response.json()["results"][0]["name"] == "Tenant A Asset"

    def test_guessed_asset_id_from_another_tenant_404s(self, client):
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        admin_a = UserFactory(tenant=tenant_a)
        upgrade_tenant_wide_role(admin_a, ROLE_ADMIN)
        admin_b = UserFactory(tenant=tenant_b)
        upgrade_tenant_wide_role(admin_b, ROLE_ADMIN)
        category_b = _make_component_category(tenant_b)

        _login(client, tenant_b, admin_b)
        other_asset_id = client.post(
            "/api/v1/assets/",
            data=json.dumps({"category": category_b.id, "name": "Secret Asset"}),
            content_type="application/json",
        ).json()["id"]
        client.post("/api/v1/auth/logout")

        _login(client, tenant_a, admin_a)
        response = client.get(f"/api/v1/assets/{other_asset_id}/")
        assert response.status_code == 404

        retire_resp = client.post(f"/api/v1/assets/{other_asset_id}/retire/")
        assert retire_resp.status_code == 404


class TestAssetScopedRBAC:
    """Core scoped-RBAC rule (docs/rbac.md §1): a ProjectLead can
    create/edit assets in THEIR OWN project only; denied on another
    project; general-pool assets require a tenant-wide grant."""

    def test_project_lead_can_create_asset_in_own_project(self, client):
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, project, ROLE_PROJECT_LEAD)
        category = _make_component_category(tenant)
        _login(client, tenant, lead)

        response = client.post(
            "/api/v1/assets/",
            data=json.dumps(
                {"category": category.id, "name": "Project Widget", "project": project.id}
            ),
            content_type="application/json",
        )
        assert response.status_code == 201, response.content
        assert response.json()["project"] == project.id

    def test_project_lead_cannot_create_asset_in_another_project(self, client):
        tenant = TenantFactory()
        own_project = ProjectFactory(tenant=tenant)
        other_project = ProjectFactory(tenant=tenant)
        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, own_project, ROLE_PROJECT_LEAD)
        category = _make_component_category(tenant)
        _login(client, tenant, lead)

        response = client.post(
            "/api/v1/assets/",
            data=json.dumps(
                {"category": category.id, "name": "Other Widget", "project": other_project.id}
            ),
            content_type="application/json",
        )
        assert response.status_code == 403

    def test_project_lead_cannot_create_general_pool_asset(self, client):
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, project, ROLE_PROJECT_LEAD)
        category = _make_component_category(tenant)
        _login(client, tenant, lead)

        response = client.post(
            "/api/v1/assets/",
            data=json.dumps({"category": category.id, "name": "General Widget"}),
            content_type="application/json",
        )
        assert response.status_code == 403

    def test_project_lead_can_edit_and_retire_asset_in_own_project(self, client):
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, project, ROLE_PROJECT_LEAD)
        category = _make_component_category(tenant)

        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        _login(client, tenant, admin)
        asset_id = client.post(
            "/api/v1/assets/",
            data=json.dumps(
                {"category": category.id, "name": "Project Widget", "project": project.id}
            ),
            content_type="application/json",
        ).json()["id"]
        client.post("/api/v1/auth/logout")

        _login(client, tenant, lead)
        edit_resp = client.patch(
            f"/api/v1/assets/{asset_id}/",
            data=json.dumps({"description": "updated by lead"}),
            content_type="application/json",
        )
        assert edit_resp.status_code == 200, edit_resp.content

        retire_resp = client.post(f"/api/v1/assets/{asset_id}/retire/")
        assert retire_resp.status_code == 200, retire_resp.content

    def test_project_lead_cannot_edit_asset_in_another_project(self, client):
        tenant = TenantFactory()
        own_project = ProjectFactory(tenant=tenant)
        other_project = ProjectFactory(tenant=tenant)
        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, own_project, ROLE_PROJECT_LEAD)
        category = _make_component_category(tenant)

        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        _login(client, tenant, admin)
        asset_id = client.post(
            "/api/v1/assets/",
            data=json.dumps(
                {"category": category.id, "name": "Other Widget", "project": other_project.id}
            ),
            content_type="application/json",
        ).json()["id"]
        client.post("/api/v1/auth/logout")

        _login(client, tenant, lead)
        edit_resp = client.patch(
            f"/api/v1/assets/{asset_id}/",
            data=json.dumps({"description": "should be denied"}),
            content_type="application/json",
        )
        assert edit_resp.status_code == 403

    def test_member_cannot_create_asset_anywhere(self, client):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)  # default: tenant-wide Member
        category = _make_component_category(tenant)
        _login(client, tenant, member)

        response = client.post(
            "/api/v1/assets/",
            data=json.dumps({"category": category.id, "name": "Should Fail"}),
            content_type="application/json",
        )
        assert response.status_code == 403

    def test_viewer_can_list_but_not_create(self, client):
        tenant = TenantFactory()
        viewer = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(viewer, ROLE_VIEWER)
        category = _make_component_category(tenant)
        _login(client, tenant, viewer)

        list_resp = client.get("/api/v1/assets/")
        assert list_resp.status_code == 200

        create_resp = client.post(
            "/api/v1/assets/",
            data=json.dumps({"category": category.id, "name": "Should Fail"}),
            content_type="application/json",
        )
        assert create_resp.status_code == 403

    def test_member_can_attach_photo_to_any_asset_footnote_1(self, client, settings, tmp_path):
        """docs/rbac.md footnote 1: Member may attach photos. `asset.attach`
        is granted to Member tenant-wide (MEMBER_PERMISSIONS), so this works
        on a general-pool asset without any project membership.

        QA (T1.9) finding: this test issues a real attachment upload (the
        same `save_attachment_file` path `TestAttachmentStorage`/
        `TestAttachmentUploadHardening` exercise below) but — unlike every
        other attachment-upload test in this file — never overrode
        `settings.MEDIA_ROOT` to `tmp_path`, so it wrote to the process
        default (`config/settings/base.py`'s `MEDIA_ROOT` default,
        `/app/media`, a path that only exists inside the deployed container).
        Outside a container with `/app` present and writable — i.e. a bare
        `pytest` run on a dev machine or (per `.github/workflows/ci.yml`'s
        `backend-test` job, which runs directly on the `ubuntu-latest`
        runner, not inside the app image) the real CI runner — this raised
        `PermissionError: [Errno 13] Permission denied: '/app'` deterministically,
        independent of anything under test here. Fixed by isolating
        `MEDIA_ROOT` per-test like its siblings.
        """
        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = _make_component_category(tenant)
        _login(client, tenant, admin)
        asset_id = client.post(
            "/api/v1/assets/",
            data=json.dumps({"category": category.id, "name": "Widget"}),
            content_type="application/json",
        ).json()["id"]
        client.post("/api/v1/auth/logout")

        member = UserFactory(tenant=tenant)
        _login(client, tenant, member)
        upload = io.BytesIO(b"bytes")
        upload.name = "note.txt"
        response = client.post(
            f"/api/v1/assets/{asset_id}/attachments/",
            data={"file": upload, "kind": "doc"},
        )
        assert response.status_code == 201, response.content


class TestAssetRelationTenantScoping:
    """R4/F1: `current_workload_user`, `category`, `project`, `location` are
    only selectable from the caller's own tenant."""

    def test_current_workload_user_from_another_tenant_is_rejected(self, client):
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        admin_a = UserFactory(tenant=tenant_a)
        upgrade_tenant_wide_role(admin_a, ROLE_ADMIN)
        other_tenant_user = UserFactory(tenant=tenant_b)
        category = _make_component_category(tenant_a)
        _login(client, tenant_a, admin_a)

        response = client.post(
            "/api/v1/assets/",
            data=json.dumps(
                {
                    "category": category.id,
                    "name": "Widget",
                    "current_workload_user": other_tenant_user.id,
                }
            ),
            content_type="application/json",
        )
        assert response.status_code == 400
        assert "current_workload_user" in response.json()["errors"]

    def test_location_from_another_tenant_is_rejected(self, client):
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        admin_a = UserFactory(tenant=tenant_a)
        upgrade_tenant_wide_role(admin_a, ROLE_ADMIN)
        other_tenant_location = LocationFactory(tenant=tenant_b, name="Room 51")
        category = _make_component_category(tenant_a)
        _login(client, tenant_a, admin_a)

        response = client.post(
            "/api/v1/assets/",
            data=json.dumps(
                {"category": category.id, "name": "Widget", "location": other_tenant_location.id}
            ),
            content_type="application/json",
        )
        assert response.status_code == 400
        assert "location" in response.json()["errors"]


class TestAssetQueryBudget:
    def test_asset_list_query_count_is_bounded(self, client, django_assert_max_num_queries):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = _make_component_category(tenant)
        _login(client, tenant, admin)
        for i in range(15):
            client.post(
                "/api/v1/assets/",
                data=json.dumps({"category": category.id, "name": f"Widget {i}"}),
                content_type="application/json",
            )

        with django_assert_max_num_queries(20):
            response = client.get("/api/v1/assets/")
        assert response.status_code == 200
        assert response.json()["count"] == 15


class TestAssetReparentingScope:
    """Code-review finding: `asset.edit` is 🟡 "only within the user's
    project" (docs/rbac.md §3) — checking only the asset's EXISTING project
    let a ProjectLead re-park an asset they can edit into a project (or the
    general pool) they do NOT hold `asset.edit` in. Fixed in
    `apps.assets.permissions.AssetPermission.has_object_permission`.
    """

    def _create_asset_in_project(self, client, category, project):
        return client.post(
            "/api/v1/assets/",
            data=json.dumps(
                {"category": category.id, "name": "Movable Widget", "project": project.id}
            ),
            content_type="application/json",
        ).json()["id"]

    def test_project_lead_cannot_move_asset_to_another_project(self, client):
        tenant = TenantFactory()
        own_project = ProjectFactory(tenant=tenant)
        other_project = ProjectFactory(tenant=tenant)
        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, own_project, ROLE_PROJECT_LEAD)
        category = _make_component_category(tenant)

        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        _login(client, tenant, admin)
        asset_id = self._create_asset_in_project(client, category, own_project)
        client.post("/api/v1/auth/logout")

        _login(client, tenant, lead)
        response = client.patch(
            f"/api/v1/assets/{asset_id}/",
            data=json.dumps({"project": other_project.id}),
            content_type="application/json",
        )
        assert response.status_code == 403, response.content

        with tenant_context(tenant.id):
            asset = Asset.objects.get(pk=asset_id)
            assert asset.project_id == own_project.id  # unchanged

    def test_project_lead_cannot_move_asset_to_general_pool(self, client):
        tenant = TenantFactory()
        own_project = ProjectFactory(tenant=tenant)
        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, own_project, ROLE_PROJECT_LEAD)
        category = _make_component_category(tenant)

        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        _login(client, tenant, admin)
        asset_id = self._create_asset_in_project(client, category, own_project)
        client.post("/api/v1/auth/logout")

        _login(client, tenant, lead)
        response = client.patch(
            f"/api/v1/assets/{asset_id}/",
            data=json.dumps({"project": None}),
            content_type="application/json",
        )
        assert response.status_code == 403, response.content

        with tenant_context(tenant.id):
            asset = Asset.objects.get(pk=asset_id)
            assert asset.project_id == own_project.id  # unchanged

    def test_admin_can_reparent_asset_freely(self, client):
        tenant = TenantFactory()
        project_a = ProjectFactory(tenant=tenant)
        project_b = ProjectFactory(tenant=tenant)
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = _make_component_category(tenant)
        _login(client, tenant, admin)

        asset_id = self._create_asset_in_project(client, category, project_a)

        move_to_b = client.patch(
            f"/api/v1/assets/{asset_id}/",
            data=json.dumps({"project": project_b.id}),
            content_type="application/json",
        )
        assert move_to_b.status_code == 200, move_to_b.content
        assert move_to_b.json()["project"] == project_b.id

        move_to_general_pool = client.patch(
            f"/api/v1/assets/{asset_id}/",
            data=json.dumps({"project": None}),
            content_type="application/json",
        )
        assert move_to_general_pool.status_code == 200, move_to_general_pool.content
        assert move_to_general_pool.json()["project"] is None

    def test_project_lead_same_project_noop_edit_still_works(self, client):
        tenant = TenantFactory()
        own_project = ProjectFactory(tenant=tenant)
        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, own_project, ROLE_PROJECT_LEAD)
        category = _make_component_category(tenant)

        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        _login(client, tenant, admin)
        asset_id = self._create_asset_in_project(client, category, own_project)
        client.post("/api/v1/auth/logout")

        _login(client, tenant, lead)
        # Sends `project` explicitly but unchanged — must not trip the
        # re-parenting guard.
        response = client.patch(
            f"/api/v1/assets/{asset_id}/",
            data=json.dumps({"project": own_project.id, "description": "still editable"}),
            content_type="application/json",
        )
        assert response.status_code == 200, response.content
        assert response.json()["description"] == "still editable"

    def test_project_lead_edit_omitting_project_still_works(self, client):
        tenant = TenantFactory()
        own_project = ProjectFactory(tenant=tenant)
        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, own_project, ROLE_PROJECT_LEAD)
        category = _make_component_category(tenant)

        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        _login(client, tenant, admin)
        asset_id = self._create_asset_in_project(client, category, own_project)
        client.post("/api/v1/auth/logout")

        _login(client, tenant, lead)
        response = client.patch(
            f"/api/v1/assets/{asset_id}/",
            data=json.dumps({"description": "still editable"}),
            content_type="application/json",
        )
        assert response.status_code == 200, response.content


class TestAttachmentUploadHardening:
    """Code-review finding: size cap + content-type/extension allowlist,
    enforced before any bytes are written to the storage backend."""

    def _asset_id(self, client, category):
        return client.post(
            "/api/v1/assets/",
            data=json.dumps({"category": category.id, "name": "Widget"}),
            content_type="application/json",
        ).json()["id"]

    def test_oversize_upload_is_rejected(self, client, settings, tmp_path):
        settings.MEDIA_ROOT = str(tmp_path)
        settings.MAX_ATTACHMENT_UPLOAD_BYTES = 10  # tiny cap for the test
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = _make_component_category(tenant)
        _login(client, tenant, admin)
        asset_id = self._asset_id(client, category)

        upload = io.BytesIO(b"x" * 1000)
        upload.name = "photo.jpg"
        response = client.post(
            f"/api/v1/assets/{asset_id}/attachments/",
            data={"file": upload, "kind": "photo"},
        )
        assert response.status_code == 400, response.content
        assert "file" in response.json()["errors"]

        with tenant_context(tenant.id):
            assert Attachment.objects.count() == 0
        # Nothing was written to the volume for a rejected oversize upload.
        assert not any(tmp_path.rglob("*photo.jpg"))

    def test_disallowed_svg_content_type_is_rejected(self, client, settings, tmp_path):
        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = _make_component_category(tenant)
        _login(client, tenant, admin)
        asset_id = self._asset_id(client, category)

        upload = io.BytesIO(b"<svg onload=alert(1)></svg>")
        upload.name = "evil.svg"
        response = client.post(
            f"/api/v1/assets/{asset_id}/attachments/",
            data={"file": upload, "kind": "photo"},
        )
        assert response.status_code == 400, response.content
        assert "file" in response.json()["errors"]

    def test_disallowed_html_content_type_is_rejected(self, client, settings, tmp_path):
        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = _make_component_category(tenant)
        _login(client, tenant, admin)
        asset_id = self._asset_id(client, category)

        upload = io.BytesIO(b"<html><script>alert(1)</script></html>")
        upload.name = "evil.html"
        response = client.post(
            f"/api/v1/assets/{asset_id}/attachments/",
            data={"file": upload, "kind": "doc"},
        )
        assert response.status_code == 400, response.content
        assert "file" in response.json()["errors"]

    def test_valid_jpeg_photo_is_accepted(self, client, settings, tmp_path):
        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = _make_component_category(tenant)
        _login(client, tenant, admin)
        asset_id = self._asset_id(client, category)

        upload = io.BytesIO(b"\xff\xd8\xff\xe0fake-jpeg-bytes")
        upload.name = "photo.jpg"
        response = client.post(
            f"/api/v1/assets/{asset_id}/attachments/",
            data={"file": upload, "kind": "photo"},
        )
        assert response.status_code == 201, response.content

    def test_valid_png_photo_is_accepted(self, client, settings, tmp_path):
        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = _make_component_category(tenant)
        _login(client, tenant, admin)
        asset_id = self._asset_id(client, category)

        upload = io.BytesIO(b"\x89PNG\r\n\x1a\nfake-png-bytes")
        upload.name = "photo.png"
        response = client.post(
            f"/api/v1/assets/{asset_id}/attachments/",
            data={"file": upload, "kind": "photo"},
        )
        assert response.status_code == 201, response.content

    def test_valid_pdf_doc_is_accepted(self, client, settings, tmp_path):
        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = _make_component_category(tenant)
        _login(client, tenant, admin)
        asset_id = self._asset_id(client, category)

        upload = io.BytesIO(b"%PDF-1.4 fake pdf bytes")
        upload.name = "manual.pdf"
        response = client.post(
            f"/api/v1/assets/{asset_id}/attachments/",
            data={"file": upload, "kind": "doc"},
        )
        assert response.status_code == 201, response.content

    def test_photo_kind_rejects_pdf_content_type(self, client, settings, tmp_path):
        """`kind="photo"` is images-only — a PDF (a valid `kind="doc"` type)
        must still be rejected under `kind="photo"`."""
        settings.MEDIA_ROOT = str(tmp_path)
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = _make_component_category(tenant)
        _login(client, tenant, admin)
        asset_id = self._asset_id(client, category)

        upload = io.BytesIO(b"%PDF-1.4 fake pdf bytes")
        upload.name = "manual.pdf"
        response = client.post(
            f"/api/v1/assets/{asset_id}/attachments/",
            data={"file": upload, "kind": "photo"},
        )
        assert response.status_code == 400, response.content
        assert "file" in response.json()["errors"]
