"""M8 Phase 2 acceptance: the Amazon scenario, end to end.

The exit criterion from `docs/tasks/M8-expense-reconciliation.md` §9:

    one SAR charge, three USD receipts, five items across two projects —
    reconciles to zero variance;
    a ProjectLead reconciles that shared charge end-to-end with no Admin help,
    while an RBAC-matrix test proves they cannot read the other project's line
    items, name or per-line amounts, and cannot edit a charge header they do
    not fully cover;
    rounding test proves splits sum exactly.

The arithmetic tests matter more than they look: every one of them is a case
where a naive implementation loses a cent, and a lost cent is exactly the
unexplained residual an auditor asks about.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
    ExpenseFactory,
    ProjectFactory,
    TenantFactory,
    UserFactory,
    add_project_membership,
    upgrade_tenant_wide_role,
)
from apps.finance.models import Payment, Purchase
from apps.finance.services import allocate_proportionally, resolve_payment
from apps.rbac.permission_keys import ROLE_ADMIN, ROLE_PROJECT_LEAD
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


# --------------------------------------------------------------------------
# The allocation primitive
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("total", "weights", "expected_sum"),
    [
        (Decimal("100.00"), [Decimal("1"), Decimal("1"), Decimal("1")], Decimal("100.00")),
        (Decimal("30.00"), [Decimal("350"), Decimal("9")], Decimal("30.00")),
        (Decimal("4631.10"), [Decimal("400"), Decimal("834.56")], Decimal("4631.10")),
        (Decimal("0.01"), [Decimal("1"), Decimal("1"), Decimal("1")], Decimal("0.01")),
        (Decimal("-50.00"), [Decimal("1"), Decimal("2")], Decimal("-50.00")),
        # All-zero weights must still distribute rather than swallow the total.
        (Decimal("10.00"), [Decimal("0"), Decimal("0")], Decimal("10.00")),
    ],
)
def test_allocate_proportionally_sums_exactly(total, weights, expected_sum):
    shares = allocate_proportionally(total, weights)
    assert sum(shares) == expected_sum
    assert len(shares) == len(weights)


def test_allocate_proportionally_is_weighted_not_even():
    """The bug this replaces: a $350 GPU and a $9 cable must not carry equal
    shipping."""
    shares = allocate_proportionally(Decimal("30.00"), [Decimal("350"), Decimal("9")])
    assert shares[0] > shares[1]
    assert sum(shares) == Decimal("30.00")


# --------------------------------------------------------------------------
# The Amazon scenario
# --------------------------------------------------------------------------


@pytest.fixture
def amazon_scenario(client):
    """One SAR 4,631.10 charge; three USD receipts; five items; two projects."""
    tenant = TenantFactory()
    with tenant_context(tenant.id):
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        grant_a = ProjectFactory(tenant=tenant)
        project_b = ProjectFactory(tenant=tenant)

        payment = Payment.objects.create(
            tenant=tenant,
            amount=Decimal("4631.10"),
            currency="SAR",
            paid_on="2026-03-02",
            account_label="Visa •4321",
            statement_ref="STMT-77",
            vendor="Amazon",
        )
        # Three shipments of one order, all priced in USD.
        r1 = Purchase.objects.create(
            tenant=tenant,
            payment=payment,
            vendor="Amazon",
            vendor_order_number="123-4567890",
            date="2026-03-02",
            currency="USD",
            total=Decimal("400.00"),
            shipping=Decimal("0.00"),
            tax=Decimal("0.00"),
        )
        r2 = Purchase.objects.create(
            tenant=tenant,
            payment=payment,
            vendor="Amazon",
            vendor_order_number="123-4567890",
            date="2026-03-03",
            currency="USD",
            total=Decimal("834.56"),
            shipping=Decimal("0.00"),
            tax=Decimal("0.00"),
        )
        r3 = Purchase.objects.create(
            tenant=tenant,
            payment=payment,
            vendor="Amazon",
            vendor_order_number="123-4567890",
            date="2026-03-04",
            currency="USD",
            total=Decimal("100.00"),
            shipping=Decimal("0.00"),
            tax=Decimal("0.00"),
        )
        # Five items across the two projects.
        ExpenseFactory(tenant=tenant, project=grant_a, purchase=r1, amount=Decimal("350.00"))
        ExpenseFactory(tenant=tenant, project=grant_a, purchase=r1, amount=Decimal("50.00"))
        ExpenseFactory(tenant=tenant, project=project_b, purchase=r2, amount=Decimal("834.56"))
        ExpenseFactory(tenant=tenant, project=project_b, purchase=r3, amount=Decimal("60.00"))
        ExpenseFactory(tenant=tenant, project=grant_a, purchase=r3, amount=Decimal("40.00"))

    return tenant, admin, grant_a, project_b, payment


def test_amazon_scenario_reconciles_to_zero_variance(amazon_scenario):
    tenant, _admin, _a, _b, payment = amazon_scenario
    with tenant_context(tenant.id):
        resolution = resolve_payment(Payment.objects.get(pk=payment.id))

    assert resolution.variance == Decimal("0.00"), "the charge is not fully accounted for"
    assert resolution.is_balanced
    assert resolution.allocated == Decimal("4631.10")

    # Every receipt balances against its own items, too.
    for purchase in resolution.purchases:
        assert purchase.is_balanced, f"receipt {purchase.purchase_id} does not balance"

    # And the settled amounts sum back to the debit exactly.
    settled = [p.settled_amount for p in resolution.purchases]
    assert all(value is not None for value in settled)
    assert sum(value for value in settled if value is not None) == Decimal("4631.10")


def test_fx_rate_is_derived_from_the_actual_debit(amazon_scenario):
    """SAR 4,631.10 settling USD 1,334.56 of receipts is a rate of ~3.47 —
    including the bank's markup. A published mid-market rate would leave a
    residual that never balances."""
    tenant, _admin, _a, _b, payment = amazon_scenario
    with tenant_context(tenant.id):
        resolution = resolve_payment(Payment.objects.get(pk=payment.id))

    rates = [p.fx_rate for p in resolution.purchases]
    assert all(rate is not None for rate in rates)
    resolved_rates = [rate for rate in rates if rate is not None]
    # All three receipts share one currency, so they share one effective rate.
    assert max(resolved_rates) - min(resolved_rates) < Decimal("0.0001")
    expected = Decimal("4631.10") / Decimal("1334.56")
    assert abs(resolved_rates[0] - expected) < Decimal("0.0001")


def test_shipping_and_tax_spread_proportionally(client):
    """A $350 GPU on a receipt with $30 shipping carries more of it than a $9
    cable does — that is what makes each line's fully-loaded cost correct."""
    tenant = TenantFactory()
    with tenant_context(tenant.id):
        project = ProjectFactory(tenant=tenant)
        purchase = Purchase.objects.create(
            tenant=tenant,
            vendor="Acme",
            date="2026-03-02",
            currency="USD",
            total=Decimal("389.00"),
            shipping=Decimal("20.00"),
            tax=Decimal("10.00"),
        )
        ExpenseFactory(tenant=tenant, project=project, purchase=purchase, amount=Decimal("350.00"))
        ExpenseFactory(tenant=tenant, project=project, purchase=purchase, amount=Decimal("9.00"))

        from apps.finance.services import resolve_purchase

        resolution = resolve_purchase(purchase, purchase.expenses.all(), None)

    # Resolved lines follow `Expense.Meta.ordering` (`-date, -id`), NOT
    # insertion order — pick them out by amount rather than by position.
    gpu = next(line for line in resolution.lines if line.amount == Decimal("350.00"))
    cable = next(line for line in resolution.lines if line.amount == Decimal("9.00"))
    assert gpu.overhead > cable.overhead
    assert gpu.overhead + cable.overhead == Decimal("30.00"), "overhead lost a cent"
    assert gpu.loaded == Decimal("350.00") + gpu.overhead
    assert resolution.is_balanced


def test_unsettled_receipt_is_a_state_not_an_error(client):
    tenant = TenantFactory()
    with tenant_context(tenant.id):
        project = ProjectFactory(tenant=tenant)
        purchase = Purchase.objects.create(
            tenant=tenant,
            vendor="Acme",
            date="2026-03-02",
            currency="USD",
            total=Decimal("100.00"),
        )
        ExpenseFactory(tenant=tenant, project=project, purchase=purchase, amount=Decimal("100.00"))

        from apps.finance.services import resolve_purchase

        resolution = resolve_purchase(purchase, purchase.expenses.all(), None)

    assert resolution.settled_amount is None
    assert resolution.fx_rate is None
    assert resolution.is_balanced, "an unsettled receipt can still balance internally"


def test_mixed_currency_charge_uses_explicit_settled_amounts(client):
    """One charge settling a USD and a EUR receipt: the app cannot infer the
    split, so the user types each settled amount off the statement."""
    tenant = TenantFactory()
    with tenant_context(tenant.id):
        project = ProjectFactory(tenant=tenant)
        payment = Payment.objects.create(
            tenant=tenant,
            amount=Decimal("1000.00"),
            currency="SAR",
            paid_on="2026-03-02",
        )
        usd = Purchase.objects.create(
            tenant=tenant,
            payment=payment,
            date="2026-03-02",
            currency="USD",
            total=Decimal("100.00"),
            settled_amount=Decimal("375.00"),
        )
        eur = Purchase.objects.create(
            tenant=tenant,
            payment=payment,
            date="2026-03-02",
            currency="EUR",
            total=Decimal("150.00"),
            settled_amount=Decimal("625.00"),
        )
        ExpenseFactory(tenant=tenant, project=project, purchase=usd, amount=Decimal("100.00"))
        ExpenseFactory(tenant=tenant, project=project, purchase=eur, amount=Decimal("150.00"))

        resolution = resolve_payment(Payment.objects.get(pk=payment.id))

    assert resolution.is_balanced
    rates = {p.currency: p.fx_rate for p in resolution.purchases}
    eur_rate = rates["EUR"]
    assert rates["USD"] == Decimal("3.75000000")
    assert eur_rate is not None
    assert eur_rate.quantize(Decimal("0.0001")) == Decimal("4.1667")


def test_partially_settled_charge_reports_the_variance(client):
    """Warn, never block: a charge whose receipts don't yet add up is a normal
    mid-entry state, surfaced as a number rather than rejected."""
    tenant = TenantFactory()
    with tenant_context(tenant.id):
        project = ProjectFactory(tenant=tenant)
        payment = Payment.objects.create(
            tenant=tenant, amount=Decimal("1000.00"), currency="SAR", paid_on="2026-03-02"
        )
        purchase = Purchase.objects.create(
            tenant=tenant,
            payment=payment,
            date="2026-03-02",
            currency="SAR",
            total=Decimal("400.00"),
            settled_amount=Decimal("400.00"),
        )
        ExpenseFactory(tenant=tenant, project=project, purchase=purchase, amount=Decimal("400.00"))
        resolution = resolve_payment(Payment.objects.get(pk=payment.id))

    assert not resolution.is_balanced
    assert resolution.variance == Decimal("600.00")


# --------------------------------------------------------------------------
# API + RBAC
# --------------------------------------------------------------------------


def test_admin_sees_the_whole_charge(client, amazon_scenario):
    tenant, admin, _a, _b, payment = amazon_scenario
    _login(client, tenant, admin)

    response = client.get(f"/api/v1/payments/{payment.id}/")
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["is_balanced"] is True
    assert body["variance"] == "0.00"
    assert len(body["purchases"]) == 3
    # Nothing is hidden from a tenant-wide holder.
    assert body["other_projects_share"] == "0"


def test_project_lead_reconciles_their_own_share_without_admin(client, amazon_scenario):
    """The user's directive: a lead must not need an Admin to reconcile their
    own paperwork."""
    tenant, _admin, grant_a, _b, payment = amazon_scenario
    with tenant_context(tenant.id):
        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, grant_a, ROLE_PROJECT_LEAD)
    _login(client, tenant, lead)

    listed = client.get("/api/v1/payments/")
    assert listed.status_code == 200, listed.content
    ids = [row["id"] for row in listed.json()["results"]]
    assert payment.id in ids, "a lead cannot see a charge that pays for their project"

    detail = client.get(f"/api/v1/payments/{payment.id}/")
    assert detail.status_code == 200, detail.content
    body = detail.json()
    # Grant A's five-item share: 350 + 50 (receipt 1) + 40 (receipt 3), in SAR.
    assert Decimal(body["my_share"]) > 0
    assert Decimal(body["other_projects_share"]) > 0
    assert Decimal(body["my_share"]) + Decimal(body["other_projects_share"]) == Decimal("4631.10")


def test_project_lead_cannot_see_the_other_projects_detail(client, amazon_scenario):
    """The §7 redaction: the remainder is one unnamed total, never a
    breakdown, never a project name."""
    tenant, _admin, grant_a, project_b, payment = amazon_scenario
    with tenant_context(tenant.id):
        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, grant_a, ROLE_PROJECT_LEAD)
    _login(client, tenant, lead)

    body = client.get(f"/api/v1/payments/{payment.id}/").json()
    serialized = str(body)
    assert project_b.name not in serialized, "the other project's NAME leaked"
    assert "834.56" not in serialized, "the other project's per-line amount leaked"


def test_charge_touching_only_other_projects_is_invisible(client, amazon_scenario):
    """Not a 403 — simply absent, like any other row outside the caller's
    scope."""
    tenant, _admin, _grant_a, project_b, _payment = amazon_scenario
    with tenant_context(tenant.id):
        other_lead = UserFactory(tenant=tenant)
        unrelated = ProjectFactory(tenant=tenant)
        add_project_membership(other_lead, unrelated, ROLE_PROJECT_LEAD)

        lone = Payment.objects.create(
            tenant=tenant, amount=Decimal("10.00"), currency="SAR", paid_on="2026-03-02"
        )
        lone_receipt = Purchase.objects.create(
            tenant=tenant, payment=lone, date="2026-03-02", currency="SAR", total=Decimal("10.00")
        )
        ExpenseFactory(
            tenant=tenant, project=project_b, purchase=lone_receipt, amount=Decimal("10.00")
        )
    _login(client, tenant, other_lead)

    listed = client.get("/api/v1/payments/")
    assert listed.status_code == 200, listed.content
    assert lone.id not in [row["id"] for row in listed.json()["results"]]

    assert client.get(f"/api/v1/payments/{lone.id}/").status_code == 404


def test_lead_cannot_edit_a_header_they_do_not_fully_cover(client, amazon_scenario):
    """Two leads sharing one charge must not be able to overwrite each other's
    record — their own LINES stay editable, the shared header does not."""
    tenant, _admin, grant_a, _b, payment = amazon_scenario
    with tenant_context(tenant.id):
        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, grant_a, ROLE_PROJECT_LEAD)
    _login(client, tenant, lead)

    response = client.patch(
        f"/api/v1/payments/{payment.id}/",
        {"amount": "9999.00"},
        content_type="application/json",
    )
    assert response.status_code == 403, response.content


def test_lead_can_edit_a_charge_they_fully_cover(client):
    tenant = TenantFactory()
    with tenant_context(tenant.id):
        lead = UserFactory(tenant=tenant)
        project = ProjectFactory(tenant=tenant)
        add_project_membership(lead, project, ROLE_PROJECT_LEAD)
        payment = Payment.objects.create(
            tenant=tenant, amount=Decimal("100.00"), currency="SAR", paid_on="2026-03-02"
        )
        purchase = Purchase.objects.create(
            tenant=tenant,
            payment=payment,
            date="2026-03-02",
            currency="SAR",
            total=Decimal("100.00"),
        )
        ExpenseFactory(tenant=tenant, project=project, purchase=purchase, amount=Decimal("100.00"))
    _login(client, tenant, lead)

    response = client.patch(
        f"/api/v1/payments/{payment.id}/",
        {"statement_ref": "STMT-99"},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content


def test_payments_are_tenant_isolated(client, amazon_scenario):
    """R4: the charge id is a valid integer in the other tenant, and must
    still 404."""
    tenant, _admin, _a, _b, payment = amazon_scenario
    other_tenant = TenantFactory()
    with tenant_context(other_tenant.id):
        intruder = UserFactory(tenant=other_tenant)
        upgrade_tenant_wide_role(intruder, ROLE_ADMIN)
    _login(client, other_tenant, intruder)

    assert client.get(f"/api/v1/payments/{payment.id}/").status_code == 404
    assert client.get("/api/v1/payments/").json()["results"] == []


def test_deleting_a_receipt_keeps_its_expense_lines(client, amazon_scenario):
    """`Expense.purchase` is SET_NULL: deleting a mis-entered receipt must
    never destroy the financial records booked against it."""
    tenant, admin, _a, _b, payment = amazon_scenario
    _login(client, tenant, admin)

    with tenant_context(tenant.id):
        receipt = Purchase.objects.filter(payment=payment).first()
        line_ids = list(receipt.expenses.values_list("id", flat=True))
    assert line_ids

    response = client.delete(f"/api/v1/purchases/{receipt.id}/")
    assert response.status_code == 204, response.content

    with tenant_context(tenant.id):
        from apps.projects.models import Expense

        survivors = Expense.objects.filter(id__in=line_ids)
        assert survivors.count() == len(line_ids)
        assert all(e.purchase_id is None for e in survivors)


def test_project_filter_scopes_charges_to_that_project(client, amazon_scenario):
    """The project hub's Expenses tab lists a project's OWN charges via
    `?project=`, which is what lets the charges live with the expenses they
    explain instead of on a separate screen."""
    tenant, admin, grant_a, project_b, payment = amazon_scenario
    _login(client, tenant, admin)

    with tenant_context(tenant.id):
        # A charge that touches neither project in the scenario.
        unrelated_project = ProjectFactory(tenant=tenant)
        other_charge = Payment.objects.create(
            tenant=tenant, amount=Decimal("10.00"), currency="SAR", paid_on="2026-04-01"
        )
        other_receipt = Purchase.objects.create(
            tenant=tenant,
            payment=other_charge,
            date="2026-04-01",
            currency="SAR",
            total=Decimal("10.00"),
        )
        ExpenseFactory(
            tenant=tenant,
            project=unrelated_project,
            purchase=other_receipt,
            amount=Decimal("10.00"),
        )

    response = client.get(f"/api/v1/payments/?project={grant_a.id}")
    assert response.status_code == 200, response.content
    ids = [row["id"] for row in response.json()["results"]]
    assert payment.id in ids
    assert other_charge.id not in ids

    # A charge spanning two projects appears under BOTH — it is one debit, and
    # each project needs it to reconcile its own share.
    from_b = client.get(f"/api/v1/payments/?project={project_b.id}")
    assert payment.id in [row["id"] for row in from_b.json()["results"]]


def test_project_filter_does_not_duplicate_a_multi_item_charge(client, amazon_scenario):
    """The filter joins through receipts and their items, so a charge with
    several matching lines must still appear once (`distinct=True`)."""
    tenant, admin, grant_a, _b, payment = amazon_scenario
    _login(client, tenant, admin)

    response = client.get(f"/api/v1/payments/?project={grant_a.id}")
    ids = [row["id"] for row in response.json()["results"]]
    assert ids.count(payment.id) == 1, "the charge was duplicated by the join"
