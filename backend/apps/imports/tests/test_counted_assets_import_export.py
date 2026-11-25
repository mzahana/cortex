"""`quantity`/`tracking` in the importer, exporter and template."""

from __future__ import annotations

import csv
import io
import json

import pytest

from apps.assets.models import Asset
from apps.audit.models import AuditLog
from apps.catalog.models import CustomFieldDef
from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
    AssetFactory,
    CategoryFactory,
    TenantFactory,
    UserFactory,
    upgrade_tenant_wide_role,
)
from apps.imports.services import default_column_mapping
from apps.rbac.permission_keys import ROLE_ADMIN
from apps.tenancy.context import tenant_context

pytestmark = pytest.mark.django_db


def _setup(client):
    tenant = TenantFactory()
    admin = UserFactory(tenant=tenant)
    upgrade_tenant_wide_role(admin, ROLE_ADMIN)
    CategoryFactory(tenant=tenant, name="Tools", default_is_consumable=False)
    CategoryFactory(tenant=tenant, name="Screws", default_is_consumable=True)
    r = client.post(
        "/api/v1/auth/login",
        {"tenant": tenant.slug, "email": admin.email, "password": DEFAULT_TEST_PASSWORD},
        content_type="application/json",
    )
    assert r.status_code == 200
    return tenant


def _import(client, capture, csv_text):
    upload = io.BytesIO(csv_text.encode())
    upload.name = "assets.csv"
    with capture(execute=True):
        r = client.post("/api/v1/imports", {"file": upload})
    assert r.status_code == 202, r.content
    import_id = r.json()["id"]
    return import_id, client.get(f"/api/v1/imports/{import_id}").json()["report"]


def _commit(client, capture, import_id):
    with capture(execute=True):
        r = client.post(f"/api/v1/imports/{import_id}/commit", {}, content_type="application/json")
    assert r.status_code == 202, r.content


class TestImportQuantity:
    def test_quantity_maps_automatically(self):
        # Only the exact headers auto-map; "Qty"/"Count" stay custom.
        assert default_column_mapping(["quantity", "Qty", "Count", "tracking"]) == {
            "quantity": "quantity",
            "Qty": "custom",
            "Count": "custom",
            "tracking": "tracking",
        }

    def test_existing_custom_key_keeps_header_custom_by_default(self):
        got = default_column_mapping(
            ["Quantity", "name"], colliding_targets=frozenset({"quantity"})
        )
        assert got == {"Quantity": "custom", "name": "name"}
        # A sheet with both new core columns is one of our exports/templates.
        ours = default_column_mapping(
            ["quantity", "tracking"], colliding_targets=frozenset({"quantity"})
        )
        assert ours == {"quantity": "quantity", "tracking": "tracking"}

    def test_quantity_gt_one_creates_counted_and_one_or_blank_individual(
        self, client, django_capture_on_commit_callbacks
    ):
        tenant = _setup(client)
        csv_text = (
            "name,category,quantity\n"
            "Screwdrivers,Tools,12\n"
            "Drill,Tools,1\n"
            "Saw,Tools,\n"
            "Pliers,Tools,3.0\n"
        )
        import_id, report = _import(client, django_capture_on_commit_callbacks, csv_text)
        assert report["invalid_count"] == 0, report
        _commit(client, django_capture_on_commit_callbacks, import_id)
        with tenant_context(tenant.id):
            got = {a.name: (a.tracking, a.quantity) for a in Asset.objects.all()}
        assert got == {
            "Screwdrivers": ("counted", 12),
            "Drill": ("individual", 1),
            "Saw": ("individual", 1),
            "Pliers": ("counted", 3),
        }

    @pytest.mark.parametrize("bad", ["0", "-2", "abc", "2.5"])
    def test_invalid_quantity_is_row_error(self, client, django_capture_on_commit_callbacks, bad):
        tenant = _setup(client)
        _, report = _import(
            client, django_capture_on_commit_callbacks, f"name,category,quantity\nX,Tools,{bad}\n"
        )
        assert report["invalid_count"] == 1
        assert "quantity" in str(report)
        with tenant_context(tenant.id):
            assert not Asset.objects.exists()

    def test_consumable_category_with_quantity_gt_one_is_row_error(
        self, client, django_capture_on_commit_callbacks
    ):
        _setup(client)
        _, report = _import(
            client,
            django_capture_on_commit_callbacks,
            "name,category,quantity\nBolts,Screws,50\nNuts,Screws,1\n",
        )
        assert report["invalid_count"] == 1
        assert report["valid_count"] == 1

    def test_explicit_tracking_rules(self, client, django_capture_on_commit_callbacks):
        _setup(client)
        csv_text = (
            "name,category,tracking,quantity\n"
            "A,Tools,counted,1\n"  # ok: count of one preserved
            "B,Tools,individual,4\n"  # conflict
            "C,Tools,weird,1\n"  # invalid
            "D,Screws,counted,2\n"  # consumable
        )
        import_id, report = _import(client, django_capture_on_commit_callbacks, csv_text)
        assert report["valid_count"] == 1 and report["invalid_count"] == 3


class TestExport:
    def _admin_asset_setup(self, client):
        tenant = _setup(client)
        cat = CategoryFactory(tenant=tenant, name="Misc")
        AssetFactory(tenant=tenant, category=cat, name="Solo")
        AssetFactory(tenant=tenant, category=cat, name="Box", tracking="counted", quantity=9)
        return tenant

    def _rows(self, response):
        body = b"".join(response.streaming_content).decode()
        return list(csv.DictReader(io.StringIO(body)))

    def test_columns_and_values(self, client):
        self._admin_asset_setup(client)
        rows = {r["name"]: r for r in self._rows(client.get("/api/v1/exports/assets.csv"))}
        assert (rows["Solo"]["tracking"], rows["Solo"]["quantity"]) == ("individual", "1")
        assert (rows["Box"]["tracking"], rows["Box"]["quantity"]) == ("counted", "9")

    def test_export_honors_tracking_filter(self, client):
        self._admin_asset_setup(client)
        rows = self._rows(client.get("/api/v1/exports/assets.csv?tracking=counted"))
        assert [r["name"] for r in rows] == ["Box"]

    def test_round_trip_preserves_counted(self, client, django_capture_on_commit_callbacks):
        tenant = self._admin_asset_setup(client)
        body = b"".join(client.get("/api/v1/exports/assets.csv").streaming_content).decode()
        assert "custom" not in default_column_mapping(body.splitlines()[0].split(",")).values()
        with tenant_context(tenant.id):
            Asset.objects.all().update(name="orig")  # so re-import isn't a duplicate
        import_id, report = _import(client, django_capture_on_commit_callbacks, body)
        assert report["invalid_count"] == 0, report
        _commit(client, django_capture_on_commit_callbacks, import_id)
        with tenant_context(tenant.id):
            got = {(a.name, a.tracking, a.quantity) for a in Asset.objects.exclude(name="orig")}
        assert got == {("Solo", "individual", 1), ("Box", "counted", 9)}


class TestCustomKeyCollisions:
    def _tenant_with_custom_quantity(self, client):
        tenant = _setup(client)
        cat = CategoryFactory(tenant=tenant, name="Cables")
        CustomFieldDef.all_objects.create(
            tenant=tenant,
            category=cat,
            key="quantity",
            label="Pack size",
            data_type=CustomFieldDef.DataType.INT,
            required=False,
        )
        return tenant, cat

    def test_import_fills_existing_custom_field_by_default(
        self, client, django_capture_on_commit_callbacks
    ):
        tenant, _ = self._tenant_with_custom_quantity(client)
        import_id, report = _import(
            client, django_capture_on_commit_callbacks, "name,category,quantity\nUSB,Cables,5\n"
        )
        assert report["invalid_count"] == 0, report
        _commit(client, django_capture_on_commit_callbacks, import_id)
        with tenant_context(tenant.id):
            asset = Asset.objects.get(name="USB")
            assert (asset.tracking, asset.quantity) == ("individual", 1)
            assert asset.field_values.get().value == 5

    def test_manual_override_to_core_target_still_possible(
        self, client, django_capture_on_commit_callbacks
    ):
        tenant, _ = self._tenant_with_custom_quantity(client)
        upload = io.BytesIO(b"name,category,quantity\nUSB,Cables,5\n")
        upload.name = "a.csv"
        with django_capture_on_commit_callbacks(execute=True):
            r = client.post(
                "/api/v1/imports",
                {"file": upload, "mapping": json.dumps({"quantity": "quantity"})},
            )
        assert r.status_code == 202, r.content
        report = client.get(f"/api/v1/imports/{r.json()['id']}").json()["report"]
        assert report["rows"][0]["values"]["quantity"] == 5
        assert report["rows"][0]["values"]["tracking"] == "counted"

    def test_export_and_template_disambiguate_and_round_trip(
        self, client, django_capture_on_commit_callbacks
    ):
        import openpyxl

        tenant, cat = self._tenant_with_custom_quantity(client)
        AssetFactory(tenant=tenant, category=cat, name="USB", tracking="counted", quantity=4)
        body = b"".join(client.get("/api/v1/exports/assets.csv").streaming_content).decode()
        header = body.splitlines()[0].split(",")
        assert header.count("quantity") == 1 and "custom:quantity" in header
        assert default_column_mapping(header)["custom:quantity"] == "custom"

        wb = openpyxl.load_workbook(
            io.BytesIO(client.get("/api/v1/exports/asset-import-template.xlsx").content)
        )
        t_header = [c.value for c in wb["Assets"][1]]
        assert t_header.count("quantity") == 1 and "custom:quantity" in t_header

        with tenant_context(tenant.id):
            Asset.objects.all().update(name="orig")
        import_id, report = _import(client, django_capture_on_commit_callbacks, body)
        assert report["invalid_count"] == 0, report
        _commit(client, django_capture_on_commit_callbacks, import_id)
        with tenant_context(tenant.id):
            asset = Asset.objects.exclude(name="orig").get(name="USB")
            assert (asset.tracking, asset.quantity) == ("counted", 4)

    def test_new_custom_field_with_core_column_key_is_400(self, client):
        tenant = _setup(client)
        cat = CategoryFactory(tenant=tenant, name="Anything")
        for key in ("quantity", "tracking", "name"):
            r = client.post(
                f"/api/v1/categories/{cat.id}/fields/",
                data=json.dumps({"key": key, "label": "L", "data_type": "int"}),
                content_type="application/json",
            )
            assert r.status_code == 400, (key, r.content)
            assert "key" in r.json()["errors"]
        ok = client.post(
            f"/api/v1/categories/{cat.id}/fields/",
            data=json.dumps({"key": "pack_size", "label": "L", "data_type": "int"}),
            content_type="application/json",
        )
        assert ok.status_code == 201, ok.content


class TestPreviewAndAudit:
    def test_dry_run_shows_tracking_and_quantity(self, client, django_capture_on_commit_callbacks):
        _setup(client)
        _, report = _import(
            client, django_capture_on_commit_callbacks, "name,category,quantity\nBox,Tools,7\n"
        )
        values = report["rows"][0]["values"]
        assert (values["tracking"], values["quantity"]) == ("counted", 7)

    def test_commit_writes_asset_create_audit_rows(
        self, client, django_capture_on_commit_callbacks
    ):
        tenant = _setup(client)
        import_id, _ = _import(
            client,
            django_capture_on_commit_callbacks,
            "name,category,quantity\nBox,Tools,7\nDrill,Tools,1\n",
        )
        _commit(client, django_capture_on_commit_callbacks, import_id)
        entries = AuditLog.all_objects.filter(
            tenant_id=tenant.id, entity_type="asset", action="asset.create"
        )
        assert entries.count() == 2
        by_id = {e.entity_id: e for e in entries}
        with tenant_context(tenant.id):
            box = Asset.objects.get(name="Box")
        after = by_id[str(box.id)].after
        assert after is not None
        assert after["tracking"] == "counted" and after["quantity"] == 7


class TestCollisionRule:
    """One tenant-wide rule: normalized key OR label equals a core target/alias."""

    def _custom(self, tenant, category, key, label="L"):
        return CustomFieldDef.all_objects.create(
            tenant=tenant,
            category=category,
            key=key,
            label=label,
            data_type=CustomFieldDef.DataType.INT,
        )

    def _rows(self, response):
        body = b"".join(response.streaming_content).decode()
        return body, list(csv.DictReader(io.StringIO(body)))

    def test_filtered_export_without_the_custom_field_still_round_trips(
        self, client, django_capture_on_commit_callbacks
    ):
        tenant = _setup(client)
        with_field = CategoryFactory(tenant=tenant, name="Cables")
        without = CategoryFactory(tenant=tenant, name="Plain")
        self._custom(tenant, with_field, "quantity", "Pack size")
        AssetFactory(tenant=tenant, category=without, name="Box", tracking="counted", quantity=6)
        body, rows = self._rows(client.get(f"/api/v1/exports/assets.csv?category={without.id}"))
        header = body.splitlines()[0].split(",")
        assert "custom:quantity" in header
        assert rows[0]["quantity"] == "6"
        with tenant_context(tenant.id):
            Asset.objects.all().update(name="orig")
        import_id, report = _import(client, django_capture_on_commit_callbacks, body)
        assert report["invalid_count"] == 0, report
        _commit(client, django_capture_on_commit_callbacks, import_id)
        with tenant_context(tenant.id):
            box = Asset.objects.exclude(name="orig").get(name="Box")
        assert (box.tracking, box.quantity) == ("counted", 6)

    def test_empty_filtered_export_still_carries_marker_column(self, client):
        tenant = _setup(client)
        cat = CategoryFactory(tenant=tenant, name="Cables")
        empty = CategoryFactory(tenant=tenant, name="Empty")
        self._custom(tenant, cat, "quantity", "Pack size")
        body, rows = self._rows(client.get(f"/api/v1/exports/assets.csv?category={empty.id}"))
        assert rows == []
        assert "custom:quantity" in body.splitlines()[0].split(",")

    def test_no_collision_means_no_prefix(self, client):
        tenant = _setup(client)
        cat = CategoryFactory(tenant=tenant, name="Cables")
        self._custom(tenant, cat, "pack_size", "Pack size")
        AssetFactory(tenant=tenant, category=cat, name="X")
        body, _ = self._rows(client.get("/api/v1/exports/assets.csv"))
        header = body.splitlines()[0].split(",")
        assert "pack_size" in header and not any(h.startswith("custom:") for h in header)

    @pytest.mark.parametrize(("key", "label"), [("qty", "Quantity"), ("Quantity", "x")])
    def test_label_or_case_collision_keeps_plain_header_custom(
        self, client, django_capture_on_commit_callbacks, key, label
    ):
        tenant = _setup(client)
        cat = CategoryFactory(tenant=tenant, name="Cables")
        self._custom(tenant, cat, key, label)
        import_id, report = _import(
            client, django_capture_on_commit_callbacks, "name,category,Quantity\nUSB,Cables,5\n"
        )
        assert report["invalid_count"] == 0, report
        _commit(client, django_capture_on_commit_callbacks, import_id)
        with tenant_context(tenant.id):
            asset = Asset.objects.get(name="USB")
        assert (asset.tracking, asset.quantity) == ("individual", 1)

    def test_label_only_collision_exports_unprefixed_and_round_trips(
        self, client, django_capture_on_commit_callbacks
    ):
        tenant = _setup(client)
        cat = CategoryFactory(tenant=tenant, name="Cables")
        self._custom(tenant, cat, "qty", "Quantity")
        AssetFactory(tenant=tenant, category=cat, name="USB", tracking="counted", quantity=3)
        body, _ = self._rows(client.get("/api/v1/exports/assets.csv"))
        header = body.splitlines()[0].split(",")
        assert "qty" in header and not any(h.startswith("custom:") for h in header)
        with tenant_context(tenant.id):
            Asset.objects.all().update(name="orig")
        import_id, report = _import(client, django_capture_on_commit_callbacks, body)
        assert report["invalid_count"] == 0, report
        _commit(client, django_capture_on_commit_callbacks, import_id)
        with tenant_context(tenant.id):
            asset = Asset.objects.exclude(name="orig").get(name="USB")
        assert (asset.tracking, asset.quantity) == ("counted", 3)

    def test_collision_fallback_is_per_row(self, client, django_capture_on_commit_callbacks):
        """Hand-made sheet in a tenant with a colliding custom field: rows whose
        category HAS the field keep it custom; rows whose category lacks it
        get the core quantity instead of losing the count."""
        tenant = _setup(client)
        with_field = CategoryFactory(tenant=tenant, name="Cables")
        CategoryFactory(tenant=tenant, name="Plain")
        self._custom(tenant, with_field, "qty", "Quantity")
        import_id, report = _import(
            client,
            django_capture_on_commit_callbacks,
            "name,category,Quantity\nUSB,Cables,5\nBox,Plain,8\n",
        )
        assert report["invalid_count"] == 0, report
        _commit(client, django_capture_on_commit_callbacks, import_id)
        with tenant_context(tenant.id):
            usb = Asset.objects.get(name="USB")
            box = Asset.objects.get(name="Box")
            assert (usb.tracking, usb.quantity) == ("individual", 1)
            assert usb.field_values.get().value == 5
            assert (box.tracking, box.quantity) == ("counted", 8)

    def test_hand_made_sheet_without_collision_maps_core(
        self, client, django_capture_on_commit_callbacks
    ):
        tenant = _setup(client)
        import_id, _ = _import(
            client, django_capture_on_commit_callbacks, "name,category,quantity\nBox,Tools,4\n"
        )
        _commit(client, django_capture_on_commit_callbacks, import_id)
        with tenant_context(tenant.id):
            assert Asset.objects.get(name="Box").quantity == 4


class TestReservedCustomKeys:
    def _post(self, client, cat, key):
        return client.post(
            f"/api/v1/categories/{cat.id}/fields/",
            data=json.dumps({"key": key, "label": "L", "data_type": "int"}),
            content_type="application/json",
        )

    @pytest.mark.parametrize("key", ["Quantity", "purchase-date", "sn", "Serial_Number", "brand"])
    def test_normalized_core_and_alias_keys_refused(self, client, key):
        tenant = _setup(client)
        cat = CategoryFactory(tenant=tenant, name="Anything")
        r = self._post(client, cat, key)
        assert r.status_code == 400, (key, r.content)
        assert "key" in r.json()["errors"]

    def test_existing_colliding_field_can_still_be_edited_but_not_rekeyed(self, client):
        tenant = _setup(client)
        cat = CategoryFactory(tenant=tenant, name="Anything")
        fd = CustomFieldDef.all_objects.create(
            tenant=tenant,
            category=cat,
            key="quantity",
            label="Old",
            data_type=CustomFieldDef.DataType.INT,
        )
        url = f"/api/v1/categories/{cat.id}/fields/{fd.id}/"
        ok = client.patch(
            url,
            data=json.dumps({"label": "New", "key": "quantity"}),
            content_type="application/json",
        )
        assert ok.status_code == 200, ok.content
        bad = client.patch(
            url, data=json.dumps({"key": "tracking"}), content_type="application/json"
        )
        assert bad.status_code == 400
