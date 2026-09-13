"""`create_missing`: letting an import CREATE the categories/locations/
projects a spreadsheet names but the tenant doesn't have yet.

The invariants worth pinning down here are the safety ones: opting in is
never implicit, a dry-run still writes nothing, an ambiguous name is never
"fixed" by creating a duplicate, existing config is never modified, and the
opt-in is gated on the same permissions the ordinary admin-CRUD screens
require rather than riding along on `import.run`.
"""

from __future__ import annotations

import io
import json

import pytest

from apps.assets.models import Asset
from apps.catalog.models import Category, Location
from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
    CategoryFactory,
    LocationFactory,
    TenantFactory,
    UserFactory,
    upgrade_tenant_wide_role,
)
from apps.projects.models import Project
from apps.rbac.models import Permission, UserPermissionOverride
from apps.rbac.permission_keys import CATEGORY_MANAGE, ROLE_ADMIN
from apps.tenancy.context import tenant_context

pytestmark = pytest.mark.django_db


def _login(client, tenant, user):
    response = client.post(
        "/api/v1/auth/login",
        {"tenant": tenant.slug, "email": user.email, "password": DEFAULT_TEST_PASSWORD},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content


def _upload(client, csv_text: str, create_missing: list[str] | None = None):
    upload = io.BytesIO(csv_text.encode("utf-8"))
    upload.name = "assets.csv"
    data: dict[str, object] = {"file": upload}
    if create_missing is not None:
        data["create_missing"] = json.dumps(create_missing)
    return client.post("/api/v1/imports", data=data)


def _report(client, import_id: int) -> dict:
    payload = client.get(f"/api/v1/imports/{import_id}").json()
    return payload["report"]


def _admin(tenant=None):
    tenant = tenant or TenantFactory()
    admin = UserFactory(tenant=tenant)
    upgrade_tenant_wide_role(admin, ROLE_ADMIN)
    return tenant, admin


class TestDryRunReportsWhatIsMissing:
    def test_unknown_names_are_errors_and_are_listed_as_missing(
        self, client, django_capture_on_commit_callbacks
    ):
        """Default behaviour is unchanged — the names are merely REPORTED so
        the UI can offer to create them."""
        tenant, admin = _admin()
        CategoryFactory(tenant=tenant, name="Compute")
        _login(client, tenant, admin)

        csv_text = (
            "name,category,location,project\n"
            "Box A,Compute,Rack 9,Grant Alpha\n"
            "Box B,Drone Kit,Rack 9,\n"
        )
        with django_capture_on_commit_callbacks(execute=True):
            response = _upload(client, csv_text)
        report = _report(client, response.json()["id"])

        assert report["invalid_count"] == 2
        assert report["missing_references"] == {
            "category": ["Drone Kit"],
            "location": ["Rack 9"],  # de-duplicated across both rows
            "project": ["Grant Alpha"],
        }
        assert report["create_missing"] == []
        rows = {r["row_number"]: r for r in report["rows"]}
        assert "location" in rows[2]["errors"]
        assert rows[2]["unresolved_references"]["location"] == "Rack 9"
        # The raw text the user typed still shows in the review table.
        assert rows[3]["values"]["category"] == "Drone Kit"

        with tenant_context(tenant.id):
            assert not Location.objects.filter(name="Rack 9").exists()

    def test_opting_in_clears_the_errors_but_still_writes_nothing(
        self, client, django_capture_on_commit_callbacks
    ):
        tenant, admin = _admin()
        CategoryFactory(tenant=tenant, name="Compute")
        _login(client, tenant, admin)

        csv_text = "name,category,location\nBox A,Compute,Rack 9\n"
        with django_capture_on_commit_callbacks(execute=True):
            response = _upload(client, csv_text, create_missing=["location"])
        report = _report(client, response.json()["id"])

        assert report["invalid_count"] == 0
        assert report["create_missing"] == ["location"]
        assert report["missing_references"]["location"] == ["Rack 9"]
        assert report["created_references"]["location"] == []
        with tenant_context(tenant.id):
            assert not Location.objects.filter(name="Rack 9").exists()

    def test_a_partial_opt_in_only_clears_that_target(
        self, client, django_capture_on_commit_callbacks
    ):
        tenant, admin = _admin()
        _login(client, tenant, admin)

        csv_text = "name,category,location\nBox A,Drone Kit,Rack 9\n"
        with django_capture_on_commit_callbacks(execute=True):
            response = _upload(client, csv_text, create_missing=["location"])
        report = _report(client, response.json()["id"])

        rows = {r["row_number"]: r for r in report["rows"]}
        assert "category" in rows[2]["errors"]
        assert "location" not in rows[2]["errors"]

    def test_an_ambiguous_name_stays_an_error_even_when_opted_in(
        self, client, django_capture_on_commit_callbacks
    ):
        """Two locations share a name — creating a third can't disambiguate
        which one the row meant."""
        tenant, admin = _admin()
        CategoryFactory(tenant=tenant, name="Compute")
        parent = LocationFactory(tenant=tenant, name="Lab")
        LocationFactory(tenant=tenant, name="Bench")
        LocationFactory(tenant=tenant, name="Bench", parent=parent)
        _login(client, tenant, admin)

        csv_text = "name,category,location\nBox A,Compute,Bench\n"
        with django_capture_on_commit_callbacks(execute=True):
            response = _upload(client, csv_text, create_missing=["location"])
        report = _report(client, response.json()["id"])

        rows = {r["row_number"]: r for r in report["rows"]}
        assert "ambiguous" in rows[2]["errors"]["location"]
        assert report["missing_references"]["location"] == []
        with tenant_context(tenant.id):
            assert Location.objects.filter(name="Bench").count() == 2


class TestCommitCreatesThem:
    def _commit(self, client, import_id, django_capture_on_commit_callbacks, create_missing=None):
        body = {} if create_missing is None else {"create_missing": create_missing}
        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(
                f"/api/v1/imports/{import_id}/commit",
                body,
                content_type="application/json",
            )
        assert response.status_code == 202, response.content
        return client.get(f"/api/v1/imports/{import_id}").json()

    def test_creates_all_three_kinds_and_links_the_assets_to_them(
        self, client, django_capture_on_commit_callbacks
    ):
        tenant, admin = _admin()
        _login(client, tenant, admin)

        csv_text = (
            "name,category,location,project\n"
            "Box A,Drone Kit,Rack 9,Grant Alpha\n"
            "Box B,Drone Kit,Rack 9,Grant Alpha\n"
        )
        with django_capture_on_commit_callbacks(execute=True):
            response = _upload(client, csv_text, create_missing=["category", "location", "project"])
        import_id = response.json()["id"]
        payload = self._commit(client, import_id, django_capture_on_commit_callbacks)

        assert payload["status"] == "committed", payload
        assert payload["report"]["created_references"] == {
            "category": ["Drone Kit"],
            "location": ["Rack 9"],
            "project": ["Grant Alpha"],
        }

        with tenant_context(tenant.id):
            # Two rows naming the same new location share ONE new row.
            assert Location.objects.filter(name="Rack 9").count() == 1
            assert Category.objects.filter(name="Drone Kit").count() == 1
            assert Project.objects.filter(name="Grant Alpha").count() == 1
            assets = list(Asset.objects.order_by("name"))
            assert [a.name for a in assets] == ["Box A", "Box B"]
            assert {a.category.name for a in assets} == {"Drone Kit"}
            assert {a.location.name for a in assets} == {"Rack 9"}
            assert {a.project.name for a in assets} == {"Grant Alpha"}
            # New tree rows land at the root for an admin to re-parent.
            assert Category.objects.get(name="Drone Kit").parent is None

    def test_existing_config_is_reused_and_never_modified(
        self, client, django_capture_on_commit_callbacks
    ):
        tenant, admin = _admin()
        category = CategoryFactory(tenant=tenant, name="Compute", requires_approval=True)
        location = LocationFactory(tenant=tenant, name="Rack 3", kind="rack")
        _login(client, tenant, admin)

        # `compute` differs only in case: it must MATCH, not create a second.
        csv_text = "name,category,location\nBox A,compute,Rack 3\n"
        with django_capture_on_commit_callbacks(execute=True):
            response = _upload(client, csv_text, create_missing=["category", "location"])
        payload = self._commit(client, response.json()["id"], django_capture_on_commit_callbacks)

        assert payload["status"] == "committed", payload
        assert payload["report"]["created_references"]["category"] == []
        with tenant_context(tenant.id):
            assert Category.objects.count() == 1
            category.refresh_from_db()
            location.refresh_from_db()
            assert category.requires_approval is True
            assert location.kind == "rack"
            assert Asset.objects.get(name="Box A").category_id == category.id

    def test_a_later_invalid_row_rolls_back_the_new_config_too(
        self, client, django_capture_on_commit_callbacks
    ):
        """All-or-nothing has to cover the config rows, not just the assets —
        otherwise a rejected import litters the tenant with half-invented
        categories."""
        tenant, admin = _admin()
        _login(client, tenant, admin)

        csv_text = (
            "name,category,location\n"
            "Box A,Drone Kit,Rack 9\n"
            ",Drone Kit,Rack 9\n"  # missing name -> whole commit fails
        )
        with django_capture_on_commit_callbacks(execute=True):
            response = _upload(client, csv_text, create_missing=["category", "location"])
        payload = self._commit(client, response.json()["id"], django_capture_on_commit_callbacks)

        assert payload["status"] == "commit_failed"
        with tenant_context(tenant.id):
            assert not Category.objects.filter(name="Drone Kit").exists()
            assert not Location.objects.filter(name="Rack 9").exists()
            assert not Asset.objects.exists()

    def test_commit_inherits_the_dry_runs_choice_when_the_body_omits_it(
        self, client, django_capture_on_commit_callbacks
    ):
        tenant, admin = _admin()
        _login(client, tenant, admin)

        csv_text = "name,category\nBox A,Drone Kit\n"
        with django_capture_on_commit_callbacks(execute=True):
            response = _upload(client, csv_text, create_missing=["category"])
        payload = self._commit(client, response.json()["id"], django_capture_on_commit_callbacks)

        assert payload["status"] == "committed", payload
        assert payload["report"]["created_references"]["category"] == ["Drone Kit"]

    def test_a_new_project_is_audited_like_an_ordinary_project_create(
        self, client, django_capture_on_commit_callbacks
    ):
        from apps.audit.models import AuditLog

        tenant, admin = _admin()
        CategoryFactory(tenant=tenant, name="Compute")
        _login(client, tenant, admin)

        csv_text = "name,category,project\nBox A,Compute,Grant Alpha\n"
        with django_capture_on_commit_callbacks(execute=True):
            response = _upload(client, csv_text, create_missing=["project"])
        self._commit(client, response.json()["id"], django_capture_on_commit_callbacks)

        with tenant_context(tenant.id):
            entry = AuditLog.objects.get(entity_type="project")
            assert entry.actor_id == admin.id
            assert entry.after["name"] == "Grant Alpha"


class TestRbacAndValidation:
    def test_import_run_alone_is_not_enough_to_create_categories(
        self, client, django_capture_on_commit_callbacks
    ):
        """An Admin explicitly DENIED `category.manage` (rbac.md §6
        per-user override) keeps `import.run` but loses the opt-in."""
        tenant, admin = _admin()
        with tenant_context(tenant.id):
            UserPermissionOverride.objects.create(
                tenant=tenant,
                user=admin,
                permission=Permission.objects.get(key=CATEGORY_MANAGE),
                effect=UserPermissionOverride.Effect.DENY,
            )
        _login(client, tenant, admin)

        response = _upload(client, "name,category\nBox A,New\n", create_missing=["category"])
        assert response.status_code == 403, response.content
        assert "category" in response.json()["detail"]

        # ... and the plain import still works.
        with django_capture_on_commit_callbacks(execute=True):
            ok = _upload(client, "name,category\nBox A,New\n")
        assert ok.status_code == 202

    def test_unknown_target_is_rejected(self, client):
        tenant, admin = _admin()
        _login(client, tenant, admin)

        response = _upload(client, "name,category\nBox A,New\n", create_missing=["tags"])
        assert response.status_code == 400, response.content

    def test_never_creates_config_for_another_tenant(
        self, client, django_capture_on_commit_callbacks
    ):
        tenant, admin = _admin()
        other = TenantFactory()
        _login(client, tenant, admin)

        csv_text = "name,category\nBox A,Drone Kit\n"
        with django_capture_on_commit_callbacks(execute=True):
            response = _upload(client, csv_text, create_missing=["category"])
        import_id = response.json()["id"]
        with django_capture_on_commit_callbacks(execute=True):
            client.post(f"/api/v1/imports/{import_id}/commit", {}, content_type="application/json")

        with tenant_context(other.id):
            assert not Category.objects.filter(name="Drone Kit").exists()
        with tenant_context(tenant.id):
            assert Category.objects.filter(name="Drone Kit").count() == 1
