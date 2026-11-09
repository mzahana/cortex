"""Foreign-currency receipts count at what the bank took, not at face value.

The bug: a USD AliExpress receipt paid by a SAR card was entered (correctly)
in USD, but the project budget summed `Expense.amount` regardless of
currency, so USD 26.68 counted as SAR 26.68 and "remaining" overstated the
money left by the whole exchange difference plus card charges. These tests
pin the fix: such lines count at their settled share of the debit, the
category breakdown still sums to `spent`, same-currency orders are unchanged,
and the order API shows the converted figures.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
    ProjectFactory,
    TenantFactory,
    UserFactory,
    upgrade_tenant_wide_role,
)
from apps.projects.models import ExpenseCategory, Project
from apps.projects.services import budget_rollup
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


@pytest.fixture
def setup(client):
    tenant = TenantFactory()
    with tenant_context(tenant.id):
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant, budget_total=Decimal("1000.00"), currency="SAR")
        equipment = ExpenseCategory.objects.create(tenant=tenant, name="FX Equipment")
        consumables = ExpenseCategory.objects.create(tenant=tenant, name="FX Consumables")
    _login(client, tenant, admin)
    return tenant, project, equipment, consumables


def _post(client, payload):
    response = client.post("/api/v1/orders/", payload, content_type="application/json")
    assert response.status_code == 201, response.content
    return response.json()


def _rollup(tenant, project):
    with tenant_context(tenant.id):
        return budget_rollup(Project.objects.get(pk=project.id))


def test_usd_receipt_counts_at_the_sar_the_bank_took(client, setup):
    tenant, project, equipment, consumables = setup
    _post(
        client,
        {
            "project": project.id,
            "vendor": "AliExpress",
            "paid_on": "2026-11-09",
            "amount": "129.41",
            "currency": "SAR",
            "splits": [
                {
                    "currency": "USD",
                    "total": "33.69",
                    "items": [
                        {"description": "M12 cable", "amount": "20.00", "category": consumables.id},
                        {"description": "Encoder", "amount": "13.69", "category": equipment.id},
                    ],
                }
            ],
        },
    )

    rollup = _rollup(tenant, project)
    assert rollup["spent"] == Decimal("129.41"), "spent must equal the bank debit, not USD 33.69"
    assert rollup["remaining"] == Decimal("870.59")
    assert sum(r["total"] for r in rollup["spend_by_category"]) == rollup["spent"]


def test_foreign_shipping_and_tax_are_converted_too(client, setup):
    tenant, project, equipment, _consumables = setup
    _post(
        client,
        {
            "project": project.id,
            "vendor": "Vendor",
            "paid_on": "2026-11-09",
            "amount": "400.00",
            "currency": "SAR",
            "splits": [
                {
                    "currency": "USD",
                    "shipping": "10.00",
                    "tax": "5.00",
                    "total": "100.00",
                    "items": [{"description": "Part", "amount": "85.00", "category": equipment.id}],
                }
            ],
        },
    )

    rollup = _rollup(tenant, project)
    assert rollup["spent"] == Decimal("400.00")
    rows = {r["category"]: r["total"] for r in rollup["spend_by_category"]}
    # 85 : 15 of SAR 400 -> 340.00 item, 60.00 shipping & tax.
    assert rows["FX Equipment"] == Decimal("340.00")
    assert rows["Shipping & tax"] == Decimal("60.00")


def test_mixed_currency_order_counts_sar_split_as_entered(client, setup):
    """A USD receipt plus a SAR card-charges line on one charge (how orders
    entered before this fix look): the SAR line keeps its value, the USD
    receipt takes the rest of the debit."""
    tenant, project, equipment, _consumables = setup
    _post(
        client,
        {
            "project": project.id,
            "vendor": "AliExpress",
            "paid_on": "2026-11-09",
            "amount": "129.41",
            "currency": "SAR",
            "splits": [
                {
                    "currency": "USD",
                    "total": "33.69",
                    "items": [
                        {"description": "Cable", "amount": "33.69", "category": equipment.id}
                    ],
                },
                {
                    "currency": "SAR",
                    "total": "2.91",
                    "items": [
                        {"description": "Card charges", "amount": "2.91", "category": equipment.id}
                    ],
                },
            ],
        },
    )
    assert _rollup(tenant, project)["spent"] == Decimal("129.41")


def test_same_currency_orders_are_unchanged(client, setup):
    """Domestic orders keep their exact figures — including an unbalanced one,
    which still counts its items + overhead, as before."""
    tenant, project, equipment, _consumables = setup
    _post(
        client,
        {
            "project": project.id,
            "vendor": "Amazon",
            "paid_on": "2026-11-09",
            "amount": "359.00",
            "currency": "SAR",
            "splits": [
                {
                    "tax": "4.00",
                    "items": [{"description": "GPU", "amount": "350.00", "category": equipment.id}],
                }
            ],
        },
    )
    rollup = _rollup(tenant, project)
    assert rollup["spent"] == Decimal("354.00")
    rows = {r["category"]: r["total"] for r in rollup["spend_by_category"]}
    assert rows == {"FX Equipment": Decimal("350.00"), "Shipping & tax": Decimal("4.00")}


def test_order_api_shows_converted_figures(client, setup):
    _tenant, project, equipment, _consumables = setup
    body = _post(
        client,
        {
            "project": project.id,
            "vendor": "AliExpress",
            "paid_on": "2026-11-09",
            "amount": "129.41",
            "currency": "SAR",
            "splits": [
                {
                    "currency": "USD",
                    "total": "33.69",
                    "items": [
                        {"description": "Cable", "amount": "33.69", "category": equipment.id}
                    ],
                },
            ],
        },
    )
    split = body["splits"][0]
    assert split["settled_total"] == "129.41"
    assert Decimal(split["fx_rate"]) == (Decimal("129.41") / Decimal("33.69")).quantize(
        Decimal("0.00000001")
    )
    assert split["items"][0]["settled"] == "129.41"


def test_same_currency_split_has_no_converted_figures(client, setup):
    _tenant, project, equipment, _consumables = setup
    body = _post(
        client,
        {
            "project": project.id,
            "vendor": "Amazon",
            "paid_on": "2026-11-09",
            "amount": "50.00",
            "currency": "SAR",
            "splits": [
                {"items": [{"description": "Cable", "amount": "50.00", "category": equipment.id}]}
            ],
        },
    )
    split = body["splits"][0]
    assert split["settled_total"] is None and split["fx_rate"] is None
    assert split["items"][0]["settled"] is None


def test_rollup_stays_query_bounded(client, setup, django_assert_max_num_queries):
    """Ten foreign orders must not cost ten times the queries."""
    tenant, project, equipment, _consumables = setup
    for i in range(10):
        _post(
            client,
            {
                "project": project.id,
                "vendor": "AliExpress",
                "paid_on": "2026-11-09",
                "amount": "37.50",
                "currency": "SAR",
                "splits": [
                    {
                        "currency": "USD",
                        "total": "10.00",
                        "items": [
                            {
                                "description": f"Item {i}",
                                "amount": "10.00",
                                "category": equipment.id,
                            }
                        ],
                    }
                ],
            },
        )
    with tenant_context(tenant.id):
        project = Project.objects.get(pk=project.id)
        with django_assert_max_num_queries(8):
            rollup = budget_rollup(project)
    assert rollup["spent"] == Decimal("375.00")


# --- Review follow-ups -------------------------------------------------------


def _usd_order(client, project, category, amount="129.41", splits=None):
    return _post(
        client,
        {
            "project": project.id,
            "vendor": "AliExpress",
            "paid_on": "2026-11-09",
            "amount": amount,
            "currency": "SAR",
            "splits": splits
            or [
                {
                    "currency": "USD",
                    "total": "33.69",
                    "items": [{"description": "Cable", "amount": "33.69", "category": category.id}],
                }
            ],
        },
    )


def test_report_expense_table_sums_to_total_spent(client, setup):
    """The report's per-order "Total cost" column must restate foreign receipts
    the same way the budget does, or the audit document contradicts itself."""
    from apps.projects.services import resolve_project_report_data

    tenant, project, equipment, _consumables = setup
    _usd_order(client, project, equipment)
    _post(
        client,
        {
            "project": project.id,
            "vendor": "Amazon",
            "paid_on": "2026-11-10",
            "amount": "54.00",
            "currency": "SAR",
            "splits": [
                {
                    "tax": "4.00",
                    "items": [{"description": "GPU", "amount": "50.00", "category": equipment.id}],
                }
            ],
        },
    )
    with tenant_context(tenant.id):
        data = resolve_project_report_data(Project.objects.get(pk=project.id))
    loaded = {row.vendor: row.loaded for row in data.expenses}
    assert loaded == {"AliExpress": Decimal("129.41"), "Amazon": Decimal("54.00")}
    assert sum(loaded.values()) == data.spent == Decimal("183.41")
    # An itemized foreign order is not flagged as unreconciled.
    (ali,) = [row for row in data.expenses if row.vendor == "AliExpress"]
    assert ali.charged == ali.loaded


def test_patch_response_and_audit_show_post_save_figures(client, setup):
    from apps.audit.models import AuditLog

    tenant, project, equipment, _consumables = setup
    order = _usd_order(client, project, equipment)
    response = client.patch(
        f"/api/v1/orders/{order['id']}/",
        {"amount": "150.00"},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["splits"][0]["settled_total"] == "150.00"
    assert body["splits"][0]["items"][0]["settled"] == "150.00"
    assert body["is_balanced"] is True
    with tenant_context(tenant.id):
        log = AuditLog.objects.filter(entity_type="order", entity_id=str(order["id"])).latest(
            "created_at"
        )
    assert log.before["splits"][0]["settled_total"] == "129.41"
    assert log.after["splits"][0]["settled_total"] == "150.00"


def test_foreign_receipt_without_items_is_unitemized_not_shipping(client, setup):
    tenant, project, _equipment, _consumables = setup
    _usd_order(
        client, project, None, amount="375.00", splits=[{"currency": "USD", "total": "100.00"}]
    )
    rollup = _rollup(tenant, project)
    rows = {r["category"]: r["total"] for r in rollup["spend_by_category"]}
    assert rows == {"Unitemized": Decimal("375.00")}
    assert rollup["spent"] == Decimal("375.00")


def test_rounding_across_lines_and_two_foreign_receipts(client, setup):
    """Odd amounts, shipping and tax, two USD receipts on one charge: spent is
    the debit to the cent and the category rows sum exactly to it."""
    tenant, project, equipment, consumables = setup
    _usd_order(
        client,
        project,
        equipment,
        amount="1000.01",
        splits=[
            {
                "currency": "USD",
                "shipping": "3.33",
                "tax": "1.11",
                "total": "101.11",
                "items": [
                    {"description": "A", "amount": "33.33", "category": equipment.id},
                    {"description": "B", "amount": "33.33", "category": consumables.id},
                    {"description": "C", "amount": "30.01", "category": consumables.id},
                ],
            },
            {
                "currency": "USD",
                "total": "166.67",
                "items": [{"description": "D", "amount": "166.67", "category": equipment.id}],
            },
        ],
    )
    rollup = _rollup(tenant, project)
    assert rollup["spent"] == Decimal("1000.01")
    assert sum(r["total"] for r in rollup["spend_by_category"]) == rollup["spent"]


def test_explicit_settled_amount_is_respected(client, setup):
    tenant, project, equipment, _consumables = setup
    _usd_order(
        client,
        project,
        equipment,
        amount="200.00",
        splits=[
            {
                "currency": "USD",
                "total": "20.00",
                "settled_amount": "75.00",
                "items": [{"description": "A", "amount": "20.00", "category": equipment.id}],
            },
            {
                "currency": "EUR",
                "total": "30.00",
                "items": [{"description": "B", "amount": "30.00", "category": equipment.id}],
            },
        ],
    )
    rollup = _rollup(tenant, project)
    assert rollup["spent"] == Decimal("200.00")
    assert {r["category"]: r["total"] for r in rollup["spend_by_category"]} == {
        "FX Equipment": Decimal("200.00")
    }


def test_another_projects_foreign_order_does_not_move_this_budget(client, setup):
    tenant, project, equipment, _consumables = setup
    with tenant_context(tenant.id):
        other = ProjectFactory(tenant=tenant, budget_total=Decimal("500.00"), currency="SAR")
    _usd_order(client, other, equipment)
    rollup = _rollup(tenant, project)
    assert rollup["spent"] == Decimal("0.00")
    assert rollup["remaining"] == Decimal("1000.00")
    assert _rollup(tenant, other)["spent"] == Decimal("129.41")
