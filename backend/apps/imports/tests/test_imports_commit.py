"""T6.1 — `POST /api/v1/imports/{id}/commit`: creates the expected assets
with correct custom-field values, all-or-nothing behavior when a dry-run
still has invalid rows, and the export -> re-import round trip.
"""

from __future__ import annotations

import io
import json

import pytest

from apps.assets.models import Asset
from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
    CategoryFactory,
    CustomFieldDefFactory,
    LocationFactory,
    ProjectFactory,
    TenantFactory,
    UserFactory,
    upgrade_tenant_wide_role,
)
from apps.rbac.permission_keys import ROLE_ADMIN
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


def _upload(client, csv_text: str, filename: str = "assets.csv", mapping: dict | None = None):
    upload = io.BytesIO(csv_text.encode("utf-8"))
    upload.name = filename
    data: dict[str, object] = {"file": upload}
    if mapping is not None:
        data["mapping"] = json.dumps(mapping)
    return client.post("/api/v1/imports", data=data)


def _dry_run(client, django_capture_on_commit_callbacks, csv_text: str, **kwargs) -> int:
    with django_capture_on_commit_callbacks(execute=True):
        response = _upload(client, csv_text, **kwargs)
    assert response.status_code == 202, response.content
    return response.json()["id"]


def _commit(
    client, django_capture_on_commit_callbacks, import_id: int, mapping: dict | None = None
):
    body = {"mapping": mapping} if mapping is not None else {}
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(
            f"/api/v1/imports/{import_id}/commit",
            data=json.dumps(body),
            content_type="application/json",
        )
    assert response.status_code == 202, response.content
    job_id = response.json()["commit_job"]["id"]
    poll = client.get(f"/api/v1/jobs/{job_id}")
    assert poll.status_code == 200, poll.content
    detail = client.get(f"/api/v1/imports/{import_id}")
    assert detail.status_code == 200, detail.content
    return poll.json(), detail.json()


class TestImportCommit:
    def test_commit_creates_assets_with_custom_fields_tags_location_project(
        self, client, django_capture_on_commit_callbacks
    ):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = CategoryFactory(tenant=tenant, name="Compute")
        CustomFieldDefFactory(
            category=category, key="vram_gb", label="VRAM (GB)", data_type="int", required=True
        )
        location = LocationFactory(tenant=tenant, name="Rack 3")
        project = ProjectFactory(tenant=tenant, name="Alpha")
        _login(client, tenant, admin)

        csv_text = (
            "name,category,location,status,condition,project,tags,vram_gb\n"
            'RTX Box A,Compute,Rack 3,available,Good,Alpha,"gpu, nvidia",24\n'
        )
        import_id = _dry_run(client, django_capture_on_commit_callbacks, csv_text)
        job_poll, detail = _commit(client, django_capture_on_commit_callbacks, import_id)

        assert job_poll["status"] == "succeeded", job_poll
        assert detail["status"] == "committed"
        assert len(detail["created_asset_ids"]) == 1

        with tenant_context(tenant.id):
            asset = Asset.objects.get(pk=detail["created_asset_ids"][0])
            assert asset.name == "RTX Box A"
            assert asset.category_id == category.id
            assert asset.location_id == location.id
            assert asset.project_id == project.id
            assert asset.status == Asset.Status.AVAILABLE
            assert asset.condition == "Good"
            assert {fv.field_def.key: fv.value for fv in asset.field_values.all()} == {
                "vram_gb": 24
            }
            assert sorted(link.tag.name for link in asset.tag_links.all()) == ["gpu", "nvidia"]

    def test_commit_is_all_or_nothing_when_a_row_is_invalid(
        self, client, django_capture_on_commit_callbacks
    ):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = CategoryFactory(tenant=tenant, name="Tools")
        _login(client, tenant, admin)

        csv_text = "name,category\nGood One,Tools\nBad One,DoesNotExist\n"
        import_id = _dry_run(client, django_capture_on_commit_callbacks, csv_text)
        job_poll, detail = _commit(client, django_capture_on_commit_callbacks, import_id)

        assert job_poll["status"] == "failed", job_poll
        assert detail["status"] == "commit_failed"
        assert detail["created_asset_ids"] == []
        assert detail["report"]["invalid_count"] == 1

        with tenant_context(tenant.id):
            assert Asset.objects.filter(category=category).count() == 0

    def test_commit_on_unknown_import_id_is_404(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        _login(client, tenant, admin)

        response = client.post(
            "/api/v1/imports/999999/commit", data="{}", content_type="application/json"
        )
        assert response.status_code == 404

    def test_commit_before_dry_run_has_succeeded_is_rejected(self, client):
        from apps.imports.models import ImportJob

        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        _login(client, tenant, admin)

        with tenant_context(tenant.id):
            import_job = ImportJob.objects.create(
                tenant=tenant,
                source_filename="assets.csv",
                source_storage_key="imports/does/not/matter.csv",
                created_by=admin,
                status=ImportJob.Status.PENDING,
            )

        response = client.post(
            f"/api/v1/imports/{import_job.id}/commit", data="{}", content_type="application/json"
        )
        assert response.status_code == 409


class TestImportExportRoundTrip:
    def test_export_then_reimport_reproduces_equivalent_assets(
        self, client, django_capture_on_commit_callbacks
    ):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = CategoryFactory(tenant=tenant, name="Compute")
        CustomFieldDefFactory(
            category=category, key="vram_gb", label="VRAM (GB)", data_type="int", required=True
        )
        location = LocationFactory(tenant=tenant, name="Rack 3")
        project = ProjectFactory(tenant=tenant, name="Alpha")
        _login(client, tenant, admin)

        original_csv = (
            "name,category,location,status,condition,project,tags,vram_gb\n"
            "RTX Box A,Compute,Rack 3,available,Good,Alpha,gpu,24\n"
            "RTX Box B,Compute,Rack 3,available,Good,Alpha,gpu,48\n"
        )
        import_id = _dry_run(client, django_capture_on_commit_callbacks, original_csv)
        job_poll, detail = _commit(client, django_capture_on_commit_callbacks, import_id)
        assert job_poll["status"] == "succeeded", job_poll
        assert len(detail["created_asset_ids"]) == 2

        export = client.get("/api/v1/exports/assets.csv?category=" + str(category.id))
        assert export.status_code == 200, export.content
        exported_csv = b"".join(export.streaming_content).decode("utf-8")

        with tenant_context(tenant.id):
            Asset.objects.filter(category=category).delete()

        reimport_id = _dry_run(client, django_capture_on_commit_callbacks, exported_csv)
        reimport_detail = client.get(f"/api/v1/imports/{reimport_id}").json()
        assert reimport_detail["report"]["valid_count"] == 2
        assert reimport_detail["report"]["invalid_count"] == 0

        _, commit_detail = _commit(client, django_capture_on_commit_callbacks, reimport_id)
        assert commit_detail["status"] == "committed"
        assert len(commit_detail["created_asset_ids"]) == 2

        with tenant_context(tenant.id):
            names_and_vram = sorted(
                (
                    asset.name,
                    {fv.field_def.key: fv.value for fv in asset.field_values.all()}.get("vram_gb"),
                    asset.location_id,
                    asset.project_id,
                )
                for asset in Asset.objects.filter(category=category)
            )
        assert names_and_vram == [
            ("RTX Box A", 24, location.id, project.id),
            ("RTX Box B", 48, location.id, project.id),
        ]
