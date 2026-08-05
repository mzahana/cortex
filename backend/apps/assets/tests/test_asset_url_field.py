"""`Asset.url` — the built-in link field (product/procurement page) that
removes the need for a per-category custom field for the near-universal case.

The one property worth real tests: the value is rendered as an `<a href>` on
the Asset Detail screen, so the write boundary must only ever store
http/https — a stored `javascript:` URL would be a click-triggered stored-XSS
vector. Both write paths (API and spreadsheet import) enforce it.
"""

from __future__ import annotations

import io

import pytest

from apps.assets.models import Asset
from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
    AssetFactory,
    CategoryFactory,
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


def _upload(client, csv_text: str):
    upload = io.BytesIO(csv_text.encode("utf-8"))
    upload.name = "assets.csv"
    return client.post("/api/v1/imports", data={"file": upload})


def _dry_run(client, django_capture_on_commit_callbacks, csv_text: str) -> int:
    """Same two-step shape `apps.imports.tests.test_imports_commit` uses: the
    dry-run parse runs in an `on_commit` callback, so it has to be captured."""
    with django_capture_on_commit_callbacks(execute=True):
        response = _upload(client, csv_text)
    assert response.status_code == 202, response.content
    return response.json()["id"]


def _commit(client, django_capture_on_commit_callbacks, import_id: int):
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(
            f"/api/v1/imports/{import_id}/commit", data="{}", content_type="application/json"
        )
    assert response.status_code == 202, response.content
    return response.json()


def _admin_client(client):
    tenant = TenantFactory()
    admin = UserFactory(tenant=tenant)
    upgrade_tenant_wide_role(admin, ROLE_ADMIN)
    _login(client, tenant, admin)
    return tenant, admin


class TestUrlWrites:
    def test_https_url_round_trips(self, client):
        tenant, _ = _admin_client(client)
        category = CategoryFactory(tenant=tenant)

        created = client.post(
            "/api/v1/assets/",
            {
                "name": "Jetson Orin",
                "category": category.id,
                "url": "https://www.nvidia.com/jetson-orin",
            },
            content_type="application/json",
        )

        assert created.status_code == 201, created.content
        assert created.json()["url"] == "https://www.nvidia.com/jetson-orin"

    def test_blank_is_allowed(self, client):
        tenant, _ = _admin_client(client)
        category = CategoryFactory(tenant=tenant)

        created = client.post(
            "/api/v1/assets/",
            {"name": "No link", "category": category.id, "url": ""},
            content_type="application/json",
        )

        assert created.status_code == 201, created.content
        assert created.json()["url"] == ""

    @pytest.mark.parametrize(
        "hostile",
        [
            "javascript:alert(document.cookie)",
            "JavaScript:alert(1)",
            "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
            "file:///etc/passwd",
        ],
    )
    def test_non_http_schemes_are_rejected(self, client, hostile):
        tenant, _ = _admin_client(client)
        category = CategoryFactory(tenant=tenant)

        created = client.post(
            "/api/v1/assets/",
            {"name": "Hostile", "category": category.id, "url": hostile},
            content_type="application/json",
        )

        assert created.status_code == 400, created.content
        assert not Asset.all_objects.filter(url=hostile).exists()

    def test_patch_cannot_smuggle_a_javascript_url(self, client):
        tenant, _ = _admin_client(client)
        category = CategoryFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=category, url="https://ok.example")

        response = client.patch(
            f"/api/v1/assets/{asset.id}/",
            {"url": "javascript:alert(1)"},
            content_type="application/json",
        )

        assert response.status_code == 400, response.content
        asset.refresh_from_db()
        assert asset.url == "https://ok.example"


class TestUrlExportImportRoundTrip:
    def test_url_is_a_core_export_column(self, client):
        tenant, _ = _admin_client(client)
        category = CategoryFactory(tenant=tenant)
        AssetFactory(
            tenant=tenant, category=category, name="Linked", url="https://vendor.example/p/1"
        )

        response = client.get("/api/v1/exports/assets.csv")

        assert response.status_code == 200, response.content
        body = b"".join(response.streaming_content).decode()
        assert "url" in body.splitlines()[0]
        assert "https://vendor.example/p/1" in body

    def test_import_maps_a_url_column_to_the_built_in_field(
        self, client, django_capture_on_commit_callbacks
    ):
        """Not a custom field: `url` is a CORE import target, so re-importing
        an export doesn't materialize a per-category custom field for it."""
        tenant, _ = _admin_client(client)
        category = CategoryFactory(tenant=tenant, name="Compute")
        import_id = _dry_run(
            client,
            django_capture_on_commit_callbacks,
            "name,category,url\n" f"Imported GPU,{category.name},https://vendor.example/gpu\n",
        )
        _commit(client, django_capture_on_commit_callbacks, import_id)

        asset = Asset.all_objects.get(tenant=tenant, name="Imported GPU")
        assert asset.url == "https://vendor.example/gpu"
        with tenant_context(tenant.id):
            assert not asset.field_values.exists()

    def test_import_rejects_a_non_http_url_cell(self, client, django_capture_on_commit_callbacks):
        tenant, _ = _admin_client(client)
        category = CategoryFactory(tenant=tenant, name="Compute")
        import_id = _dry_run(
            client,
            django_capture_on_commit_callbacks,
            f"name,category,url\nBad,{category.name},javascript:alert(1)\n",
        )

        report = client.get(f"/api/v1/imports/{import_id}").json()["report"]

        assert report["invalid_count"] == 1
        assert "url" in report["rows"][0]["errors"]
