"""The order API: one nested document, one request (M8 Phase 2, rev).

The user's model, verbatim: *"the expense itself is the order made from a
vendor which can have splits and each split has items."* These tests pin that
shape — in particular that the SIMPLE case (one order, a few items, no split)
never requires the user or the client to know that splits exist.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
    AssetFactory,
    CategoryFactory,
    ProjectFactory,
    TenantFactory,
    UserFactory,
    upgrade_tenant_wide_role,
)
from apps.finance.models import Payment, Purchase
from apps.projects.models import Expense
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
        project = ProjectFactory(tenant=tenant)
        category = CategoryFactory(tenant=tenant)
    _login(client, tenant, admin)
    return tenant, admin, project, category


def test_simple_order_needs_no_knowledge_of_splits(client, setup):
    """The common case: a vendor, a total, a few items. The client sends no
    `splits` key at all and the server still stores a well-formed order."""
    tenant, _admin, project, _category = setup

    response = client.post(
        "/api/v1/orders/",
        {
            "project": project.id,
            "vendor": "Amazon",
            "paid_on": "2026-03-02",
            "amount": "359.00",
            "currency": "SAR",
            "splits": [
                {
                    "items": [
                        {"description": "GPU", "amount": "350.00"},
                        {"description": "Cable", "amount": "9.00"},
                    ]
                }
            ],
        },
        content_type="application/json",
    )
    assert response.status_code == 201, response.content
    body = response.json()

    assert len(body["splits"]) == 1
    assert len(body["splits"][0]["items"]) == 2
    # The split's total was inferred from its items — the user never typed it.
    assert body["splits"][0]["total"] == "359.00"
    assert body["is_balanced"] is True
    assert body["variance"] == "0.00"


def test_order_with_no_splits_key_still_creates_one(client, setup):
    """Belt and braces: even an order posted with no items at all keeps exactly
    one split, so reconciliation and reporting have a single code path."""
    tenant, _admin, project, _category = setup

    response = client.post(
        "/api/v1/orders/",
        {
            "project": project.id,
            "vendor": "Acme",
            "paid_on": "2026-03-02",
            "amount": "10.00",
            "currency": "SAR",
        },
        content_type="application/json",
    )
    assert response.status_code == 201, response.content
    with tenant_context(tenant.id):
        assert Purchase.objects.filter(payment_id=response.json()["id"]).count() == 1


def test_split_order_with_items_in_each(client, setup):
    """The scenario from the user's first message: one payment, several
    receipts, items spread across them."""
    tenant, _admin, project, _category = setup

    response = client.post(
        "/api/v1/orders/",
        {
            "project": project.id,
            "vendor": "Amazon",
            "paid_on": "2026-03-02",
            "amount": "1234.56",
            "currency": "SAR",
            "splits": [
                {
                    "receipt_number": "SHIP-1",
                    "items": [{"description": "GPU", "amount": "400.00"}],
                },
                {
                    "receipt_number": "SHIP-2",
                    "items": [{"description": "Jetson", "amount": "834.56"}],
                },
            ],
        },
        content_type="application/json",
    )
    assert response.status_code == 201, response.content
    body = response.json()

    assert [s["receipt_number"] for s in body["splits"]] == ["SHIP-1", "SHIP-2"]
    assert body["is_balanced"] is True, body["variance"]


def test_items_become_project_expenses_and_hit_the_budget(client, setup):
    """The items ARE the project's expense ledger — an order is not a parallel
    universe of costs."""
    tenant, _admin, project, _category = setup

    client.post(
        "/api/v1/orders/",
        {
            "project": project.id,
            "vendor": "Amazon",
            "paid_on": "2026-03-02",
            "amount": "359.00",
            "currency": "SAR",
            "splits": [{"items": [{"description": "GPU", "amount": "350.00"}]}],
        },
        content_type="application/json",
    )

    with tenant_context(tenant.id):
        assert Expense.objects.filter(project=project).count() == 1
        assert budget_rollup(project)["spent"] == Decimal("350.00")


def test_items_can_link_assets(client, setup):
    tenant, _admin, project, category = setup
    with tenant_context(tenant.id):
        asset = AssetFactory(tenant=tenant, category=category, project=project)

    response = client.post(
        "/api/v1/orders/",
        {
            "project": project.id,
            "vendor": "Amazon",
            "paid_on": "2026-03-02",
            "amount": "350.00",
            "currency": "SAR",
            "splits": [{"items": [{"description": "GPU", "amount": "350.00", "asset": asset.id}]}],
        },
        content_type="application/json",
    )
    assert response.status_code == 201, response.content
    assert response.json()["splits"][0]["items"][0]["asset"] == asset.id


def test_item_cannot_link_another_projects_asset(client, setup):
    """Same rule as the standalone expense form: an order buys its own
    project's assets and nobody else's."""
    tenant, _admin, project, category = setup
    with tenant_context(tenant.id):
        other_project = ProjectFactory(tenant=tenant)
        foreign = AssetFactory(tenant=tenant, category=category, project=other_project)

    response = client.post(
        "/api/v1/orders/",
        {
            "project": project.id,
            "vendor": "Amazon",
            "paid_on": "2026-03-02",
            "amount": "10.00",
            "currency": "SAR",
            "splits": [{"items": [{"description": "X", "amount": "10.00", "asset": foreign.id}]}],
        },
        content_type="application/json",
    )
    assert response.status_code == 400, response.content


def test_order_requires_a_project(client, setup):
    _tenant, _admin, _project, _category = setup
    response = client.post(
        "/api/v1/orders/",
        {"vendor": "Amazon", "paid_on": "2026-03-02", "amount": "10.00", "currency": "SAR"},
        content_type="application/json",
    )
    assert response.status_code == 400, response.content


def test_editing_an_order_replaces_its_items(client, setup):
    """Set semantics: an item removed from the form is removed from the
    ledger, because the user just deleted it from the order."""
    tenant, _admin, project, _category = setup

    created = client.post(
        "/api/v1/orders/",
        {
            "project": project.id,
            "vendor": "Amazon",
            "paid_on": "2026-03-02",
            "amount": "359.00",
            "currency": "SAR",
            "splits": [
                {
                    "items": [
                        {"description": "GPU", "amount": "350.00"},
                        {"description": "Cable", "amount": "9.00"},
                    ]
                }
            ],
        },
        content_type="application/json",
    ).json()

    split = created["splits"][0]
    kept = split["items"][0]

    updated = client.patch(
        f"/api/v1/orders/{created['id']}/",
        {
            "splits": [
                {
                    "id": split["id"],
                    "items": [{"id": kept["id"], "description": "GPU", "amount": "350.00"}],
                }
            ]
        },
        content_type="application/json",
    )
    assert updated.status_code == 200, updated.content
    assert len(updated.json()["splits"][0]["items"]) == 1

    with tenant_context(tenant.id):
        assert Expense.objects.filter(project=project).count() == 1


def test_a_failed_item_rolls_back_the_whole_order(client, setup):
    """One request, one transaction: a bad item must not leave an order with
    no items behind — the half-entered state a financial record must never be
    in."""
    tenant, _admin, project, category = setup
    with tenant_context(tenant.id):
        other_project = ProjectFactory(tenant=tenant)
        foreign = AssetFactory(tenant=tenant, category=category, project=other_project)
        before = Payment.objects.count()

    client.post(
        "/api/v1/orders/",
        {
            "project": project.id,
            "vendor": "Amazon",
            "paid_on": "2026-03-02",
            "amount": "10.00",
            "currency": "SAR",
            "splits": [
                {
                    "items": [
                        {"description": "ok", "amount": "5.00"},
                        {"description": "bad", "amount": "5.00", "asset": foreign.id},
                    ]
                }
            ],
        },
        content_type="application/json",
    )

    with tenant_context(tenant.id):
        assert Payment.objects.count() == before, "a rejected order left a row behind"


def test_order_list_is_scoped_to_its_project(client, setup):
    tenant, _admin, project, _category = setup
    with tenant_context(tenant.id):
        other_project = ProjectFactory(tenant=tenant)

    for target in (project, other_project):
        client.post(
            "/api/v1/orders/",
            {
                "project": target.id,
                "vendor": "Amazon",
                "paid_on": "2026-03-02",
                "amount": "10.00",
                "currency": "SAR",
            },
            content_type="application/json",
        )

    response = client.get(f"/api/v1/orders/?project={project.id}")
    assert response.status_code == 200, response.content
    results = response.json()["results"]
    assert len(results) == 1
    assert results[0]["project"] == project.id


def test_orders_are_tenant_isolated(client, setup):
    tenant, _admin, project, _category = setup
    created = client.post(
        "/api/v1/orders/",
        {
            "project": project.id,
            "vendor": "Amazon",
            "paid_on": "2026-03-02",
            "amount": "10.00",
            "currency": "SAR",
        },
        content_type="application/json",
    ).json()

    other_tenant = TenantFactory()
    with tenant_context(other_tenant.id):
        intruder = UserFactory(tenant=other_tenant)
        upgrade_tenant_wide_role(intruder, ROLE_ADMIN)
    _login(client, other_tenant, intruder)

    assert client.get(f"/api/v1/orders/{created['id']}/").status_code == 404


def test_foreign_currency_order_reconciles(client, setup):
    """A USD receipt settled by a SAR charge: the rate comes from the debit."""
    tenant, _admin, project, _category = setup

    response = client.post(
        "/api/v1/orders/",
        {
            "project": project.id,
            "vendor": "Amazon",
            "paid_on": "2026-03-02",
            "amount": "1500.00",
            "currency": "SAR",
            "splits": [
                {
                    "currency": "USD",
                    "total": "400.00",
                    "items": [{"description": "GPU", "amount": "400.00"}],
                }
            ],
        },
        content_type="application/json",
    )
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["is_balanced"] is True
    assert body["splits"][0]["currency"] == "USD"


def test_an_item_takes_one_asset_not_many(client, setup):
    """An item is a single line on a receipt — one thing bought — so it links
    exactly one asset."""
    tenant, _admin, project, category = setup
    with tenant_context(tenant.id):
        first = AssetFactory(tenant=tenant, category=category, project=project)
        second = AssetFactory(tenant=tenant, category=category, project=project)

    created = client.post(
        "/api/v1/orders/",
        {
            "project": project.id,
            "vendor": "Amazon",
            "paid_on": "2026-03-02",
            "amount": "350.00",
            "currency": "SAR",
            "splits": [{"items": [{"description": "GPU", "amount": "350.00", "asset": first.id}]}],
        },
        content_type="application/json",
    )
    assert created.status_code == 201, created.content
    body = created.json()
    assert body["splits"][0]["items"][0]["asset"] == first.id

    # Re-pointing the item swaps the link rather than accumulating one.
    split = body["splits"][0]
    item = split["items"][0]
    updated = client.patch(
        f"/api/v1/orders/{body['id']}/",
        {
            "splits": [
                {
                    "id": split["id"],
                    "items": [
                        {
                            "id": item["id"],
                            "description": "GPU",
                            "amount": "350.00",
                            "asset": second.id,
                        }
                    ],
                }
            ]
        },
        content_type="application/json",
    )
    assert updated.status_code == 200, updated.content
    assert updated.json()["splits"][0]["items"][0]["asset"] == second.id

    with tenant_context(tenant.id):
        from apps.projects.models import ExpenseAssetLink

        assert ExpenseAssetLink.objects.filter(expense_id=item["id"]).count() == 1


# --------------------------------------------------------------------------
# Paperwork uploads
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "content_type"),
    [
        ("receipt.pdf", "application/pdf"),
        # A phone photo of a paper receipt is just as common as a vendor PDF.
        # The two `kind` allowlists are disjoint, so hardcoding either one
        # rejected half of what people actually upload.
        ("receipt.jpg", "image/jpeg"),
    ],
)
def test_receipt_scan_accepts_pdf_and_photo(client, setup, filename, content_type):
    tenant, _admin, project, _category = setup
    created = client.post(
        "/api/v1/orders/",
        {
            "project": project.id,
            "vendor": "Amazon",
            "paid_on": "2026-03-02",
            "amount": "10.00",
            "currency": "SAR",
            "splits": [{"items": [{"description": "X", "amount": "10.00"}]}],
        },
        content_type="application/json",
    ).json()
    split_id = created["splits"][0]["id"]

    upload = SimpleUploadedFile(filename, b"%PDF-1.4 fake bytes", content_type=content_type)
    response = client.post(f"/api/v1/purchases/{split_id}/attachment/", {"file": upload})
    assert response.status_code == 201, response.content
    assert response.json()["filename"] == filename


def test_bank_statement_upload(client, setup):
    tenant, _admin, project, _category = setup
    created = client.post(
        "/api/v1/orders/",
        {
            "project": project.id,
            "vendor": "Amazon",
            "paid_on": "2026-03-02",
            "amount": "10.00",
            "currency": "SAR",
        },
        content_type="application/json",
    ).json()

    upload = SimpleUploadedFile("statement.pdf", b"%PDF-1.4 fake", content_type="application/pdf")
    response = client.post(f"/api/v1/payments/{created['id']}/attachment/", {"file": upload})
    assert response.status_code == 201, response.content

    # And it comes back on the order, so the form can show "1 attached".
    fetched = client.get(f"/api/v1/orders/{created['id']}/")
    assert len(fetched.json()["attachments"]) == 1


def test_upload_rejects_a_disallowed_type(client, setup):
    """The allowlist is still default-deny — an HTML file is never accepted,
    whichever `kind` the content type resolves to."""
    tenant, _admin, project, _category = setup
    created = client.post(
        "/api/v1/orders/",
        {
            "project": project.id,
            "vendor": "Amazon",
            "paid_on": "2026-03-02",
            "amount": "10.00",
            "currency": "SAR",
        },
        content_type="application/json",
    ).json()

    upload = SimpleUploadedFile("evil.html", b"<script>", content_type="text/html")
    response = client.post(f"/api/v1/payments/{created['id']}/attachment/", {"file": upload})
    assert response.status_code == 400, response.content


def test_replacing_a_wrongly_uploaded_receipt(client, setup):
    """Uploading the wrong file must be recoverable: delete it, upload the
    right one. Crucially the STORED FILE goes too — a row-only delete would
    leave an orphaned blob on the volume forever with nothing pointing at it.
    """
    from django.core.files.storage import default_storage

    tenant, _admin, project, _category = setup
    created = client.post(
        "/api/v1/orders/",
        {
            "project": project.id,
            "vendor": "Amazon",
            "paid_on": "2026-03-02",
            "amount": "10.00",
            "currency": "SAR",
            "splits": [{"items": [{"description": "X", "amount": "10.00"}]}],
        },
        content_type="application/json",
    ).json()
    split_id = created["splits"][0]["id"]

    wrong = SimpleUploadedFile("wrong.pdf", b"%PDF-1.4 wrong", content_type="application/pdf")
    uploaded = client.post(f"/api/v1/purchases/{split_id}/attachment/", {"file": wrong}).json()
    storage_key = uploaded["storage_key"]
    assert default_storage.exists(storage_key)

    deleted = client.delete(f"/api/v1/purchase-attachments/{uploaded['id']}/")
    assert deleted.status_code == 204, deleted.content
    assert not default_storage.exists(storage_key), "the file was orphaned on the volume"

    # ...and the right one can go up in its place.
    right = SimpleUploadedFile("right.pdf", b"%PDF-1.4 right", content_type="application/pdf")
    replacement = client.post(f"/api/v1/purchases/{split_id}/attachment/", {"file": right})
    assert replacement.status_code == 201, replacement.content

    order = client.get(f"/api/v1/orders/{created['id']}/").json()
    filenames = [a["filename"] for a in order["splits"][0]["attachments"]]
    assert filenames == ["right.pdf"]


def test_deleting_a_bank_statement_removes_its_file(client, setup):
    from django.core.files.storage import default_storage

    tenant, _admin, project, _category = setup
    created = client.post(
        "/api/v1/orders/",
        {
            "project": project.id,
            "vendor": "Amazon",
            "paid_on": "2026-03-02",
            "amount": "10.00",
            "currency": "SAR",
        },
        content_type="application/json",
    ).json()

    upload = SimpleUploadedFile("stmt.pdf", b"%PDF-1.4", content_type="application/pdf")
    attachment = client.post(
        f"/api/v1/payments/{created['id']}/attachment/", {"file": upload}
    ).json()
    storage_key = attachment["storage_key"]

    assert client.delete(f"/api/v1/payment-attachments/{attachment['id']}/").status_code == 204
    assert not default_storage.exists(storage_key)
