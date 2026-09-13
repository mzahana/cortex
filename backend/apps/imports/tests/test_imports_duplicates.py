"""Duplicate handling: an import must never silently create a second copy of
an asset the tenant already has.

The default (`on_duplicate="reject"`) is a behaviour CHANGE — before this,
re-uploading the same sheet duplicated every row without a word. These tests
pin down the default, the two explicit escapes (`skip`/`create`), and the
in-file case (the same row twice in one sheet).
"""

from __future__ import annotations

import io

import pytest

from apps.assets.models import Asset
from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
    CategoryFactory,
    TenantFactory,
    UserFactory,
    upgrade_tenant_wide_role,
)
from apps.rbac.permission_keys import ROLE_ADMIN
from apps.tenancy.context import tenant_context

pytestmark = pytest.mark.django_db

CSV = "name,category\nJetson Orin,Compute\n"


def _login(client, tenant, user):
    response = client.post(
        "/api/v1/auth/login",
        {"tenant": tenant.slug, "email": user.email, "password": DEFAULT_TEST_PASSWORD},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content


def _setup(client):
    tenant = TenantFactory()
    admin = UserFactory(tenant=tenant)
    upgrade_tenant_wide_role(admin, ROLE_ADMIN)
    category = CategoryFactory(tenant=tenant, name="Compute")
    _login(client, tenant, admin)
    return tenant, admin, category


def _upload(client, csv_text: str, on_duplicate: str | None = None):
    upload = io.BytesIO(csv_text.encode("utf-8"))
    upload.name = "assets.csv"
    data: dict[str, object] = {"file": upload}
    if on_duplicate is not None:
        data["on_duplicate"] = on_duplicate
    return client.post("/api/v1/imports", data=data)


def _commit(client, import_id, capture, on_duplicate: str | None = None):
    body = {} if on_duplicate is None else {"on_duplicate": on_duplicate}
    with capture(execute=True):
        response = client.post(
            f"/api/v1/imports/{import_id}/commit", body, content_type="application/json"
        )
    assert response.status_code == 202, response.content
    return client.get(f"/api/v1/imports/{import_id}").json()


def _dry_run(client, capture, csv_text=CSV, on_duplicate=None):
    with capture(execute=True):
        response = _upload(client, csv_text, on_duplicate)
    assert response.status_code == 202, response.content
    payload = client.get(f"/api/v1/imports/{response.json()['id']}").json()
    return payload["id"], payload["report"]


class TestDefaultRejects:
    def test_re_uploading_the_same_sheet_is_rejected_not_duplicated(
        self, client, django_capture_on_commit_callbacks
    ):
        tenant, _, category = _setup(client)
        with tenant_context(tenant.id):
            existing = Asset.objects.create(tenant=tenant, category=category, name="Jetson Orin")

        import_id, report = _dry_run(client, django_capture_on_commit_callbacks)

        assert report["on_duplicate"] == "reject"
        assert report["invalid_count"] == 1
        assert report["duplicate_count"] == 1
        entry = report["duplicate_rows"][0]
        assert entry["row_number"] == 2
        assert entry["name"] == "Jetson Orin"
        assert entry["existing_asset_ids"] == [existing.id]
        assert "already exists" in report["rows"][0]["errors"]["duplicate"]

        payload = _commit(client, import_id, django_capture_on_commit_callbacks)
        assert payload["status"] == "commit_failed"
        with tenant_context(tenant.id):
            assert Asset.objects.count() == 1

    def test_matching_is_case_insensitive_on_name_and_category(
        self, client, django_capture_on_commit_callbacks
    ):
        tenant, _, category = _setup(client)
        with tenant_context(tenant.id):
            Asset.objects.create(tenant=tenant, category=category, name="Jetson Orin")

        _, report = _dry_run(
            client, django_capture_on_commit_callbacks, "name,category\n  jetson ORIN ,compute\n"
        )
        assert report["duplicate_count"] == 1

    def test_the_same_name_in_a_different_category_is_not_a_duplicate(
        self, client, django_capture_on_commit_callbacks
    ):
        tenant, _, _ = _setup(client)
        other = CategoryFactory(tenant=tenant, name="Spares")
        with tenant_context(tenant.id):
            Asset.objects.create(tenant=tenant, category=other, name="Jetson Orin")

        _, report = _dry_run(client, django_capture_on_commit_callbacks)
        assert report["duplicate_count"] == 0
        assert report["invalid_count"] == 0

    def test_another_tenants_identical_asset_is_not_a_duplicate(
        self, client, django_capture_on_commit_callbacks
    ):
        tenant, _, _ = _setup(client)
        other_tenant = TenantFactory()
        other_category = CategoryFactory(tenant=other_tenant, name="Compute")
        with tenant_context(other_tenant.id):
            Asset.objects.create(tenant=other_tenant, category=other_category, name="Jetson Orin")

        _, report = _dry_run(client, django_capture_on_commit_callbacks)
        assert report["duplicate_count"] == 0

    def test_two_identical_rows_in_one_file_flag_the_second(
        self, client, django_capture_on_commit_callbacks
    ):
        _setup(client)
        csv_text = "name,category\nJetson Orin,Compute\nJetson Orin,Compute\n"

        _, report = _dry_run(client, django_capture_on_commit_callbacks, csv_text)

        assert report["duplicate_count"] == 1
        entry = report["duplicate_rows"][0]
        assert entry["row_number"] == 3
        assert entry["duplicate_of_row"] == 2
        assert entry["existing_asset_ids"] == []
        assert "Row 2 of this file" in report["rows"][1]["errors"]["duplicate"]


class TestDuplicatesUnderANewCategory:
    """Regression: a row whose category is being CREATED by this same import
    has no resolved `Category` yet, so duplicate detection has to fall back
    to the raw name — otherwise two identical rows naming a brand-new
    category both sail through and get created."""

    def test_two_identical_rows_naming_a_new_category_are_still_flagged(
        self, client, django_capture_on_commit_callbacks
    ):
        tenant, _, _ = _setup(client)
        csv_text = "name,category\nDrone A,Quadrotor\nDrone A,Quadrotor\n"

        upload = io.BytesIO(csv_text.encode("utf-8"))
        upload.name = "assets.csv"
        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(
                "/api/v1/imports",
                {"file": upload, "create_missing": '["category"]'},
            )
        assert response.status_code == 202, response.content
        import_id = response.json()["id"]
        report = client.get(f"/api/v1/imports/{import_id}").json()["report"]

        assert report["duplicate_count"] == 1
        assert report["duplicate_rows"][0]["duplicate_of_row"] == 2
        assert report["duplicate_rows"][0]["category"] == "Quadrotor"
        assert report["invalid_count"] == 1  # blocked by the `reject` default

        payload = _commit(client, import_id, django_capture_on_commit_callbacks)
        assert payload["status"] == "commit_failed"
        with tenant_context(tenant.id):
            assert Asset.objects.count() == 0

    def test_skip_creates_one_asset_and_one_new_category(
        self, client, django_capture_on_commit_callbacks
    ):
        from apps.catalog.models import Category

        tenant, _, _ = _setup(client)
        csv_text = "name,category\nDrone A,Quadrotor\nDrone A,Quadrotor\n"

        upload = io.BytesIO(csv_text.encode("utf-8"))
        upload.name = "assets.csv"
        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(
                "/api/v1/imports",
                {"file": upload, "create_missing": '["category"]', "on_duplicate": "skip"},
            )
        import_id = response.json()["id"]
        payload = _commit(client, import_id, django_capture_on_commit_callbacks)

        assert payload["status"] == "committed", payload
        with tenant_context(tenant.id):
            assert Asset.objects.filter(name="Drone A").count() == 1
            assert Category.objects.filter(name="Quadrotor").count() == 1


class TestExplicitChoices:
    def test_skip_imports_the_rest_and_reports_what_it_skipped(
        self, client, django_capture_on_commit_callbacks
    ):
        tenant, _, category = _setup(client)
        with tenant_context(tenant.id):
            Asset.objects.create(tenant=tenant, category=category, name="Jetson Orin")
        csv_text = "name,category\nJetson Orin,Compute\nRaspberry Pi 5,Compute\n"

        import_id, report = _dry_run(
            client, django_capture_on_commit_callbacks, csv_text, on_duplicate="skip"
        )
        assert report["invalid_count"] == 0
        assert report["skipped_count"] == 1
        assert report["duplicate_rows"][0]["skipped"] is True

        payload = _commit(client, import_id, django_capture_on_commit_callbacks)
        assert payload["status"] == "committed", payload
        assert len(payload["created_asset_ids"]) == 1
        with tenant_context(tenant.id):
            assert sorted(Asset.objects.values_list("name", flat=True)) == [
                "Jetson Orin",
                "Raspberry Pi 5",
            ]

    def test_skip_with_every_row_already_present_is_a_success_not_a_failure(
        self, client, django_capture_on_commit_callbacks
    ):
        tenant, _, category = _setup(client)
        with tenant_context(tenant.id):
            Asset.objects.create(tenant=tenant, category=category, name="Jetson Orin")

        import_id, _ = _dry_run(client, django_capture_on_commit_callbacks, on_duplicate="skip")
        payload = _commit(client, import_id, django_capture_on_commit_callbacks)

        assert payload["status"] == "committed", payload
        assert payload["created_asset_ids"] == []
        assert payload["report"]["skipped_count"] == 1
        with tenant_context(tenant.id):
            assert Asset.objects.count() == 1

    def test_create_makes_the_second_copy_but_still_flags_it(
        self, client, django_capture_on_commit_callbacks
    ):
        """A lab really can buy a second identical Jetson."""
        tenant, _, category = _setup(client)
        with tenant_context(tenant.id):
            Asset.objects.create(tenant=tenant, category=category, name="Jetson Orin")

        import_id, report = _dry_run(
            client, django_capture_on_commit_callbacks, on_duplicate="create"
        )
        assert report["invalid_count"] == 0
        assert report["duplicate_count"] == 1  # still reported, just not blocking

        payload = _commit(client, import_id, django_capture_on_commit_callbacks)
        assert payload["status"] == "committed", payload
        with tenant_context(tenant.id):
            assert Asset.objects.filter(name="Jetson Orin").count() == 2

    def test_commit_inherits_the_dry_runs_choice(self, client, django_capture_on_commit_callbacks):
        tenant, _, category = _setup(client)
        with tenant_context(tenant.id):
            Asset.objects.create(tenant=tenant, category=category, name="Jetson Orin")

        import_id, _ = _dry_run(client, django_capture_on_commit_callbacks, on_duplicate="create")
        payload = _commit(client, import_id, django_capture_on_commit_callbacks)  # empty body

        assert payload["status"] == "committed", payload
        assert payload["report"]["on_duplicate"] == "create"

    def test_commit_can_override_the_dry_runs_choice(
        self, client, django_capture_on_commit_callbacks
    ):
        """The commit re-validates from scratch, so tightening at commit time
        must actually bite rather than replay the permissive dry run."""
        tenant, _, category = _setup(client)
        with tenant_context(tenant.id):
            Asset.objects.create(tenant=tenant, category=category, name="Jetson Orin")

        import_id, _ = _dry_run(client, django_capture_on_commit_callbacks, on_duplicate="create")
        payload = _commit(
            client, import_id, django_capture_on_commit_callbacks, on_duplicate="reject"
        )

        assert payload["status"] == "commit_failed"
        with tenant_context(tenant.id):
            assert Asset.objects.count() == 1

    def test_an_unknown_choice_is_rejected(self, client):
        _setup(client)
        assert _upload(client, CSV, on_duplicate="nuke").status_code == 400


class TestQueryBudget:
    def test_duplicate_detection_costs_one_query_regardless_of_row_count(
        self, client, django_capture_on_commit_callbacks, django_assert_num_queries
    ):
        from apps.imports.services import (  # noqa: F401
            annotate_duplicates,
            build_report,
            resolve_import_rows,
        )

        tenant, _, category = _setup(client)
        with tenant_context(tenant.id):
            for index in range(20):
                Asset.objects.create(tenant=tenant, category=category, name=f"Asset {index}")
            rows_csv = "name,category\n" + "".join(
                f"Asset {index},Compute\n" for index in range(20)
            )
            resolved = list(
                resolve_import_rows(
                    tenant=tenant,
                    source_stream=io.BytesIO(rows_csv.encode()),
                    filename="a.csv",
                    mapping_override=None,
                )
            )
            with django_assert_num_queries(1):
                annotate_duplicates(resolved, on_duplicate="reject")
            assert sum(1 for row in resolved if row.is_duplicate) == 20
