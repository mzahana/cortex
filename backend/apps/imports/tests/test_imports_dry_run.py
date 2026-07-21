"""T6.1 — `POST /api/v1/imports` dry-run: a mix of valid/invalid rows
produces a correct per-row report (resolved mapping, values, errors, and
valid/invalid counts), auto-mapping without an explicit override, and an
explicit mapping override.
"""

from __future__ import annotations

import io
import json

import pytest

from apps.catalog.models import Category
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


def _poll_dry_run(client, django_capture_on_commit_callbacks, response):
    assert response.status_code == 202, response.content
    body = response.json()
    job_id = body["dry_run_job"]["id"]
    poll = client.get(f"/api/v1/jobs/{job_id}")
    assert poll.status_code == 200, poll.content
    return body["id"], poll.json()


class TestImportDryRun:
    def test_valid_and_invalid_rows_report_correctly(
        self, client, django_capture_on_commit_callbacks
    ):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = CategoryFactory(tenant=tenant, name="Compute")
        CustomFieldDefFactory(
            category=category, key="vram_gb", label="VRAM (GB)", data_type="int", required=True
        )
        LocationFactory(tenant=tenant, name="Rack 3")
        _login(client, tenant, admin)

        csv_text = (
            "name,category,location,status,condition,project,tags,vram_gb\n"
            "RTX Box A,Compute,Rack 3,available,Good,,gpu,24\n"
            "Bad Row,UnknownCategory,,,,,,\n"
            "Missing VRAM,Compute,,,,,,\n"
            ",Compute,,,,,,10\n"
        )

        with django_capture_on_commit_callbacks(execute=True):
            response = _upload(client, csv_text)
        import_id, job_body = _poll_dry_run(client, django_capture_on_commit_callbacks, response)
        assert job_body["status"] == "succeeded", job_body

        detail = client.get(f"/api/v1/imports/{import_id}")
        assert detail.status_code == 200, detail.content
        payload = detail.json()
        assert payload["status"] == "dry_run_succeeded"
        report = payload["report"]
        assert report["total_rows"] == 4
        assert report["valid_count"] == 1
        assert report["invalid_count"] == 3

        rows = {r["row_number"]: r for r in report["rows"]}
        # Row 2 (first data row) is valid.
        assert rows[2]["errors"] == {}
        assert rows[2]["values"]["name"] == "RTX Box A"
        assert rows[2]["values"]["category"] == "Compute"
        assert rows[2]["values"]["location"] == "Rack 3"
        assert rows[2]["values"]["custom_field_values"] == {"vram_gb": 24}

        # Row 3: unknown category.
        assert "category" in rows[3]["errors"]
        # Row 4: category resolved but required custom field missing.
        assert "custom_field_values" in rows[4]["errors"]
        # Row 5: name missing.
        assert "name" in rows[5]["errors"]

        assert payload["mapping"]["name"] == "name"
        assert payload["mapping"]["category"] == "category"
        assert payload["mapping"]["vram_gb"] == "custom"

    def test_explicit_mapping_override_wins_over_auto_match(
        self, client, django_capture_on_commit_callbacks
    ):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        CategoryFactory(tenant=tenant, name="Tools")
        _login(client, tenant, admin)

        # "asset_name" doesn't auto-match anything -> defaults to "custom";
        # override it to "name" explicitly.
        csv_text = "asset_name,category\nWrench,Tools\n"
        with django_capture_on_commit_callbacks(execute=True):
            response = _upload(client, csv_text, mapping={"asset_name": "name"})
        import_id, job_body = _poll_dry_run(client, django_capture_on_commit_callbacks, response)
        assert job_body["status"] == "succeeded", job_body

        detail = client.get(f"/api/v1/imports/{import_id}").json()
        assert detail["mapping"]["asset_name"] == "name"
        assert detail["report"]["valid_count"] == 1
        assert detail["report"]["rows"][0]["values"]["name"] == "Wrench"

    def test_ambiguous_category_name_is_reported(self, client, django_capture_on_commit_callbacks):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        parent_a = CategoryFactory(tenant=tenant, name="Parent A")
        parent_b = CategoryFactory(tenant=tenant, name="Parent B")
        with tenant_context(tenant.id):
            Category.all_objects.create(tenant=tenant, parent=parent_a, name="Shared")
            Category.all_objects.create(tenant=tenant, parent=parent_b, name="Shared")
        _login(client, tenant, admin)

        csv_text = "name,category\nThing,Shared\n"
        with django_capture_on_commit_callbacks(execute=True):
            response = _upload(client, csv_text)
        import_id, job_body = _poll_dry_run(client, django_capture_on_commit_callbacks, response)
        assert job_body["status"] == "succeeded"

        detail = client.get(f"/api/v1/imports/{import_id}").json()
        assert detail["report"]["invalid_count"] == 1
        assert "ambiguous" in detail["report"]["rows"][0]["errors"]["category"]

    def test_unsupported_extension_is_rejected(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        _login(client, tenant, admin)

        response = _upload(client, "name,category\nX,Y\n", filename="assets.txt")
        assert response.status_code == 400

    def test_project_column_is_matched_by_name(self, client, django_capture_on_commit_callbacks):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        CategoryFactory(tenant=tenant, name="Drones")
        project = ProjectFactory(tenant=tenant, name="Alpha")
        _login(client, tenant, admin)

        csv_text = f"name,category,project\nDrone 1,Drones,{project.name}\n"
        with django_capture_on_commit_callbacks(execute=True):
            response = _upload(client, csv_text)
        import_id, job_body = _poll_dry_run(client, django_capture_on_commit_callbacks, response)
        assert job_body["status"] == "succeeded"

        detail = client.get(f"/api/v1/imports/{import_id}").json()
        assert detail["report"]["valid_count"] == 1
        assert detail["report"]["rows"][0]["values"]["project"] == "Alpha"
