"""The legacy-expense wrap migration (`finance.0003`).

The bug it fixes: the Expenses tab reaches an expense through its order, so
every pre-M8 expense (`purchase IS NULL`) vanished from the screen while still
counting towards the project's budget. Counted but invisible is the worst state
a financial record can be in — the budget and the screen disagree with no way
for the user to reconcile them.

Driven through the migration's own functions with the live app registry, so the
real logic is exercised rather than a copy of it.
"""

from __future__ import annotations

import importlib
from decimal import Decimal

import pytest
from django.apps import apps as live_apps

from apps.common.tests.factories import ExpenseFactory, ProjectFactory, TenantFactory
from apps.finance.models import Payment
from apps.projects.models import Expense
from apps.projects.services import budget_rollup
from apps.tenancy.context import tenant_context

# The migration module's name starts with a digit, so it cannot be imported
# with a normal `import` statement.
_wrap_migration = importlib.import_module(
    "apps.finance.migrations.0003_wrap_legacy_expenses_in_orders"
)
wrap_orphan_expenses = _wrap_migration.wrap_orphan_expenses
unwrap_orphan_expenses = _wrap_migration.unwrap_orphan_expenses

pytestmark = pytest.mark.django_db


def test_orphan_expense_becomes_a_visible_order_without_changing_the_budget():
    tenant = TenantFactory()
    with tenant_context(tenant.id):
        project = ProjectFactory(tenant=tenant)
        expense = ExpenseFactory(
            tenant=tenant,
            project=project,
            amount=Decimal("42.00"),
            currency="USD",
            vendor="OldVendor",
        )
        spent_before = budget_rollup(project)["spent"]

    wrap_orphan_expenses(live_apps, None)

    with tenant_context(tenant.id):
        expense.refresh_from_db()
        assert expense.purchase_id is not None, "the expense is still invisible on the tab"

        order = expense.purchase.payment
        # Every figure comes from the expense — nothing is invented.
        assert order.amount == Decimal("42.00")
        assert order.currency == "USD"
        assert order.vendor == "OldVendor"
        assert order.project_id == project.id
        assert order.paid_on == expense.date

        # And the budget is untouched: this is a visibility fix, not a
        # re-statement of what the project has spent.
        assert budget_rollup(project)["spent"] == spent_before


def test_wrap_is_idempotent():
    """Re-running must not create a second order for the same expense."""
    tenant = TenantFactory()
    with tenant_context(tenant.id):
        project = ProjectFactory(tenant=tenant)
        ExpenseFactory(tenant=tenant, project=project, amount=Decimal("10.00"))

    wrap_orphan_expenses(live_apps, None)
    wrap_orphan_expenses(live_apps, None)

    with tenant_context(tenant.id):
        assert Payment.objects.count() == 1


def test_reverse_detaches_without_deleting_the_expense():
    tenant = TenantFactory()
    with tenant_context(tenant.id):
        project = ProjectFactory(tenant=tenant)
        expense = ExpenseFactory(tenant=tenant, project=project, amount=Decimal("10.00"))

    wrap_orphan_expenses(live_apps, None)
    unwrap_orphan_expenses(live_apps, None)

    with tenant_context(tenant.id):
        expense.refresh_from_db()
        assert expense.purchase_id is None
        assert Payment.objects.count() == 0
        # The cost itself survived — the reverse only ever detaches.
        assert Expense.objects.filter(pk=expense.pk).exists()
