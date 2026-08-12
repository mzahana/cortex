"""Order numbering (M8): "Order N" must mean the same thing in the project
report, in the UI and in a downloaded pack filename — otherwise the number is
worse than no number at all, because it invites a wrong cross-reference.
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
from apps.finance.models import Payment
from apps.finance.services import order_number, order_pack_filename
from apps.rbac.permission_keys import ROLE_ADMIN
from apps.tenancy.context import tenant_context

pytestmark = pytest.mark.django_db


def _order(tenant, project, *, vendor, paid_on, amount="100.00"):
    return Payment.all_objects.create(
        tenant=tenant,
        project=project,
        vendor=vendor,
        paid_on=paid_on,
        amount=Decimal(amount),
        currency="SAR",
    )


def test_orders_are_numbered_in_statement_order():
    tenant = TenantFactory()
    with tenant_context(tenant.id):
        project = ProjectFactory(tenant=tenant)
        # Created out of order on purpose: the number follows the STATEMENT
        # date, not the order rows were entered in.
        second = _order(tenant, project, vendor="Amazon", paid_on="2026-03-05")
        first = _order(tenant, project, vendor="AliExpress", paid_on="2026-03-01")

        assert order_number(first) == 1
        assert order_number(second) == 2


def test_numbering_restarts_per_project():
    """The number is a position within ITS project, so two projects both have
    an Order 1 — which is what makes it readable in a project report."""
    tenant = TenantFactory()
    with tenant_context(tenant.id):
        a = ProjectFactory(tenant=tenant)
        b = ProjectFactory(tenant=tenant)
        order_a = _order(tenant, a, vendor="Amazon", paid_on="2026-03-01")
        order_b = _order(tenant, b, vendor="Amazon", paid_on="2026-03-02")

        assert order_number(order_a) == 1
        assert order_number(order_b) == 1


def test_two_orders_on_the_same_day_are_ordered_by_id():
    """A stable tiebreak, or the numbering would shuffle between renders."""
    tenant = TenantFactory()
    with tenant_context(tenant.id):
        project = ProjectFactory(tenant=tenant)
        first = _order(tenant, project, vendor="Amazon", paid_on="2026-03-01")
        second = _order(tenant, project, vendor="AliExpress", paid_on="2026-03-01")

        assert order_number(first) == 1
        assert order_number(second) == 2


def test_pack_filename_leads_with_the_zero_padded_number():
    """Zero-padded so a folder of downloaded packs sorts into statement
    order rather than 1, 10, 11, 2."""
    tenant = TenantFactory()
    with tenant_context(tenant.id):
        project = ProjectFactory(tenant=tenant)
        order = _order(tenant, project, vendor="AliExpress", paid_on="2026-03-01")

        assert order_pack_filename(order) == "order-01-aliexpress-2026-03-01.pdf"


def test_pack_filename_survives_a_hostile_vendor_name():
    tenant = TenantFactory()
    with tenant_context(tenant.id):
        project = ProjectFactory(tenant=tenant)
        order = _order(tenant, project, vendor="../../etc/passwd  &co", paid_on="2026-03-01")

        name = order_pack_filename(order)
        assert "/" not in name and ".." not in name
        assert name.endswith(".pdf")


def test_pack_filename_handles_a_blank_vendor():
    tenant = TenantFactory()
    with tenant_context(tenant.id):
        project = ProjectFactory(tenant=tenant)
        order = _order(tenant, project, vendor="", paid_on="2026-03-01")

        assert order_pack_filename(order) == "order-01-unknown-vendor-2026-03-01.pdf"


def test_the_api_returns_the_same_number_without_an_n_plus_one(client, django_assert_num_queries):
    tenant = TenantFactory()
    with tenant_context(tenant.id):
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant)
        for n in range(1, 6):
            _order(tenant, project, vendor=f"V{n}", paid_on=f"2026-03-0{n}")

    response = client.post(
        "/api/v1/auth/login",
        {"tenant": tenant.slug, "email": admin.email, "password": DEFAULT_TEST_PASSWORD},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content

    listing = client.get(f"/api/v1/orders/?project={project.id}")
    assert listing.status_code == 200, listing.content
    numbers = sorted(row["number"] for row in listing.json()["results"])
    assert numbers == [1, 2, 3, 4, 5]
