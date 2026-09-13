"""The full `Asset` scalar column set in the importer/exporter/template.

The original T6.1 schema was designed blind (Q8: no sample spreadsheet) and
covered only 8 columns, so a serial number, purchase cost or supplier typed
into a sheet was silently demoted to a per-category custom field — or
dropped. These tests pin the remaining columns down end-to-end, including
the header spellings a real inventory sheet actually uses.
"""

from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal

import pytest

from apps.assets.models import Asset
from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
    CategoryFactory,
    TenantFactory,
    UserFactory,
    upgrade_tenant_wide_role,
)
from apps.imports.services import default_column_mapping
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


def _setup(client):
    tenant = TenantFactory()
    admin = UserFactory(tenant=tenant)
    upgrade_tenant_wide_role(admin, ROLE_ADMIN)
    CategoryFactory(tenant=tenant, name="Compute")
    _login(client, tenant, admin)
    return tenant, admin


def _import(client, capture, csv_text: str):
    upload = io.BytesIO(csv_text.encode("utf-8"))
    upload.name = "assets.csv"
    with capture(execute=True):
        response = client.post("/api/v1/imports", {"file": upload})
    assert response.status_code == 202, response.content
    import_id = response.json()["id"]
    return import_id, client.get(f"/api/v1/imports/{import_id}").json()["report"]


def _commit(client, capture, import_id):
    with capture(execute=True):
        response = client.post(
            f"/api/v1/imports/{import_id}/commit", {}, content_type="application/json"
        )
    assert response.status_code == 202, response.content
    return client.get(f"/api/v1/imports/{import_id}").json()


class TestHeaderMatching:
    def test_machine_names_map_to_themselves(self):
        headers = [
            "name",
            "serial_number",
            "manufacturer",
            "model",
            "description",
            "supplier",
            "purchase_cost",
            "currency",
            "purchase_date",
            "warranty_expiry",
        ]
        assert default_column_mapping(headers) == {h: h for h in headers}

    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            ("Serial #", "serial_number"),
            ("Serial Number", "serial_number"),
            ("SERIAL-NO", "serial_number"),
            ("S/N", "serial_number"),
            ("Manufacturer", "manufacturer"),
            ("Make", "manufacturer"),
            ("Brand", "manufacturer"),
            ("Model", "model"),
            ("Model No.", "model"),
            ("Description", "description"),
            ("Supplier", "supplier"),
            ("Vendor", "supplier"),
            ("Purchased From", "supplier"),
            ("Purchase Cost", "purchase_cost"),
            ("Cost", "purchase_cost"),
            ("Unit Price", "purchase_cost"),
            ("Currency", "currency"),
            ("Purchase Date", "purchase_date"),
            ("Date Purchased", "purchase_date"),
            ("Warranty Expiry", "warranty_expiry"),
            ("Warranty", "warranty_expiry"),
            ("Asset Name", "name"),
        ],
    )
    def test_human_spellings_map_to_the_right_column(self, header, expected):
        assert default_column_mapping([header])[header] == expected

    def test_an_unknown_header_is_still_a_custom_field(self):
        assert default_column_mapping(["Coolant Type"])["Coolant Type"] == "custom"


class TestImportingTheFields:
    def test_a_full_row_round_trips_into_the_asset(
        self, client, django_capture_on_commit_callbacks
    ):
        tenant, _ = _setup(client)
        csv_text = (
            "Name,Category,Serial #,Manufacturer,Model,Description,"
            "Supplier,Purchase Cost,Currency,Purchase Date,Warranty Expiry\n"
            "Jetson Orin,Compute,SN-00042,NVIDIA,AGX 64GB,Dev board for the swarm,"
            "Mouser,1999.00,usd,2026-03-14,2028-03-14\n"
        )
        import_id, report = _import(client, django_capture_on_commit_callbacks, csv_text)
        assert report["invalid_count"] == 0, report["rows"]

        payload = _commit(client, django_capture_on_commit_callbacks, import_id)
        assert payload["status"] == "committed", payload

        with tenant_context(tenant.id):
            asset = Asset.objects.get(name="Jetson Orin")
        assert asset.serial_number == "SN-00042"
        assert asset.manufacturer == "NVIDIA"
        assert asset.model == "AGX 64GB"
        assert asset.description == "Dev board for the swarm"
        assert asset.supplier == "Mouser"
        assert asset.purchase_cost == Decimal("1999.00")
        assert asset.currency == "USD"  # normalized to the ISO upper-case form
        assert asset.purchase_date == date(2026, 3, 14)
        assert asset.warranty_expiry == date(2028, 3, 14)

    def test_money_tolerates_spreadsheet_formatting(
        self, client, django_capture_on_commit_callbacks
    ):
        tenant, _ = _setup(client)
        csv_text = 'name,category,purchase_cost\nBox,Compute,"$1,299.50"\n'
        import_id, report = _import(client, django_capture_on_commit_callbacks, csv_text)
        assert report["invalid_count"] == 0, report["rows"]

        _commit(client, django_capture_on_commit_callbacks, import_id)
        with tenant_context(tenant.id):
            assert Asset.objects.get(name="Box").purchase_cost == Decimal("1299.50")

    @pytest.mark.parametrize(
        ("column", "value", "fragment"),
        [
            ("purchase_cost", "not-a-number", "is not a number"),
            ("purchase_cost", "-5", "cannot be negative"),
            ("purchase_cost", "1.2345", "2 decimal places"),
            ("purchase_cost", "999999999999", "too large"),
            ("currency", "US Dollars", "3-letter currency code"),
            ("purchase_date", "14/03/2026", "YYYY-MM-DD"),
            ("warranty_expiry", "next year", "YYYY-MM-DD"),
        ],
    )
    def test_bad_values_are_row_errors_not_a_failed_commit(
        self, client, django_capture_on_commit_callbacks, column, value, fragment
    ):
        """Each of these would otherwise surface as a database error halfway
        through the commit, with no indication of which cell caused it."""
        _setup(client)
        csv_text = f"name,category,{column}\nBox,Compute,{value}\n"

        _, report = _import(client, django_capture_on_commit_callbacks, csv_text)

        assert report["invalid_count"] == 1
        assert fragment.lower() in str(report["rows"][0]["errors"][column]).lower()

    @pytest.mark.parametrize("column", ["name", "serial_number", "manufacturer"])
    def test_an_over_long_value_is_a_row_error(
        self, client, django_capture_on_commit_callbacks, column
    ):
        _setup(client)
        csv_text = f"name,category,{column}\nBox,Compute,{'x' * 256}\n"
        if column == "name":
            csv_text = f"name,category\n{'x' * 256},Compute\n"

        _, report = _import(client, django_capture_on_commit_callbacks, csv_text)

        assert report["invalid_count"] == 1
        assert "maximum is 255" in str(report["rows"][0]["errors"][column])

    def test_blank_cells_leave_the_fields_empty(self, client, django_capture_on_commit_callbacks):
        tenant, _ = _setup(client)
        csv_text = (
            "name,category,serial_number,purchase_cost,currency,purchase_date\n" "Box,Compute,,,,\n"
        )
        import_id, report = _import(client, django_capture_on_commit_callbacks, csv_text)
        assert report["invalid_count"] == 0

        _commit(client, django_capture_on_commit_callbacks, import_id)
        with tenant_context(tenant.id):
            asset = Asset.objects.get(name="Box")
        assert asset.serial_number == ""
        assert asset.currency == ""
        assert asset.purchase_cost is None
        assert asset.purchase_date is None


class TestExportRoundTrip:
    def test_export_emits_every_field_and_re_imports_cleanly(
        self, client, django_capture_on_commit_callbacks
    ):
        tenant, _ = _setup(client)
        csv_text = (
            "name,category,serial_number,manufacturer,model,description,supplier,"
            "purchase_cost,currency,purchase_date,warranty_expiry\n"
            "Jetson Orin,Compute,SN-1,NVIDIA,AGX,Desc,Mouser,10.00,USD,"
            "2026-01-02,2027-01-02\n"
        )
        import_id, _ = _import(client, django_capture_on_commit_callbacks, csv_text)
        _commit(client, django_capture_on_commit_callbacks, import_id)

        response = client.get("/api/v1/exports/assets.csv")
        assert response.status_code == 200
        body = b"".join(response.streaming_content).decode("utf-8")
        rows = list(csv.DictReader(io.StringIO(body)))
        assert rows[0]["serial_number"] == "SN-1"
        assert rows[0]["purchase_cost"] == "10.00"
        assert rows[0]["currency"] == "USD"
        assert rows[0]["purchase_date"] == "2026-01-02"
        assert rows[0]["warranty_expiry"] == "2027-01-02"
        assert rows[0]["supplier"] == "Mouser"

        # Feed the export straight back in: every column must auto-map (the
        # round-trip invariant `apps.imports.exports` documents).
        header = list(rows[0].keys())
        assert "custom" not in default_column_mapping(header).values()


class TestTemplateCarriesTheFields:
    def test_template_header_includes_every_asset_column(
        self, client, django_capture_on_commit_callbacks
    ):
        import openpyxl

        _setup(client)
        response = client.get("/api/v1/exports/asset-import-template.xlsx")
        assert response.status_code == 200
        workbook = openpyxl.load_workbook(io.BytesIO(response.content))
        header = [cell.value for cell in workbook["Assets"][1]]

        for column in (
            "serial_number",
            "manufacturer",
            "model",
            "description",
            "supplier",
            "purchase_cost",
            "currency",
            "purchase_date",
            "warranty_expiry",
        ):
            assert column in header
