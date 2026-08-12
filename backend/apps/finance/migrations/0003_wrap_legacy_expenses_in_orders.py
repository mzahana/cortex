"""M8 Phase 2 (rev) — give every existing expense an order to live in.

**The bug this fixes.** The project's Expenses tab now lists *orders*, and an
expense reaches it through `Expense.purchase -> Purchase.payment`. Every
expense created before this milestone has `purchase = NULL`, so it vanished
from the tab — while still counting towards the project's budget, because
`budget_rollup` sums `Expense.amount` directly. Money that is counted but
invisible is the worst possible state for a financial record: the budget says
one thing and the screen shows another, with no way for the user to reconcile
the difference.

**What it does.** Each orphan expense (`purchase IS NULL`) is wrapped in a
one-shipment order whose figures are taken from the expense itself — nothing is
invented:

    Payment.amount   <- Expense.amount        (what was paid)
    Payment.currency <- Expense.currency      (falling back to the project's)
    Payment.paid_on  <- Expense.date
    Payment.vendor   <- Expense.vendor
    Payment.project  <- Expense.project

so the resulting order says exactly what the expense already said. The user
sees a uniform list; there is no second class of "legacy expense" to explain.

Expenses whose project is NULL are skipped — an order requires a project, and
there is nothing to infer one from. In practice there are none, since
`Expense.project` is non-nullable; the guard is defensive.

**Reversible and non-destructive.** The reverse detaches the expenses and
deletes only the orders this migration created (identified by carrying exactly
one shipment with exactly one item and a matching total). No `Expense` row is
created, modified beyond its `purchase` FK, or deleted in either direction.
"""

from __future__ import annotations

from django.db import migrations


def wrap_orphan_expenses(apps_registry, schema_editor):
    Expense = apps_registry.get_model("projects", "Expense")
    Payment = apps_registry.get_model("finance", "Payment")
    Purchase = apps_registry.get_model("finance", "Purchase")

    orphans = (
        Expense._base_manager.filter(purchase__isnull=True, project__isnull=False)
        .select_related("project")
        .iterator(chunk_size=500)
    )

    for expense in orphans:
        currency = expense.currency or getattr(expense.project, "currency", "") or "USD"
        order = Payment._base_manager.create(
            tenant_id=expense.tenant_id,
            project_id=expense.project_id,
            amount=expense.amount,
            currency=currency,
            paid_on=expense.date,
            vendor=expense.vendor or "",
            notes="",
            method="other",
        )
        shipment = Purchase._base_manager.create(
            tenant_id=expense.tenant_id,
            payment=order,
            vendor=expense.vendor or "",
            receipt_number=expense.invoice_number or "",
            date=expense.date,
            currency=currency,
            subtotal=expense.amount,
            shipping=0,
            tax=0,
            total=expense.amount,
        )
        expense.purchase = shipment
        expense.save(update_fields=["purchase"])


def unwrap_orphan_expenses(apps_registry, schema_editor):
    """Detach the expenses and delete only the orders created above.

    An order qualifies as "created by this migration" when it holds exactly one
    shipment holding exactly one item whose amount equals the order's. That is
    narrow enough not to touch an order the user built by hand, and the expense
    itself is only ever detached, never deleted.
    """
    Expense = apps_registry.get_model("projects", "Expense")
    Payment = apps_registry.get_model("finance", "Payment")
    Purchase = apps_registry.get_model("finance", "Purchase")

    # Every traversal here goes through `_base_manager` explicitly. Reverse
    # accessors (`order.purchases`) must NOT be used: Django builds a related
    # manager from the model's DEFAULT manager, which for every tenant-owned
    # model in this codebase is the fail-closed `TenantScopedManager` — so
    # `order.purchases.all()` raises `TenantContextError` inside a migration,
    # which runs with no tenant in context. (Caught by
    # `test_reverse_detaches_without_deleting_the_expense`.)
    shipments_by_order: dict[int, list] = {}
    for shipment in Purchase._base_manager.filter(payment__isnull=False).iterator(chunk_size=500):
        shipments_by_order.setdefault(shipment.payment_id, []).append(shipment)

    items_by_shipment: dict[int, list] = {}
    for item in Expense._base_manager.filter(purchase__isnull=False).iterator(chunk_size=500):
        items_by_shipment.setdefault(item.purchase_id, []).append(item)

    for order in Payment._base_manager.iterator(chunk_size=500):
        shipments = shipments_by_order.get(order.id, [])
        if len(shipments) != 1:
            continue
        items = items_by_shipment.get(shipments[0].id, [])
        if len(items) != 1 or items[0].amount != order.amount:
            continue

        Expense._base_manager.filter(pk=items[0].pk).update(purchase=None)
        Purchase._base_manager.filter(pk=shipments[0].pk).delete()
        Payment._base_manager.filter(pk=order.pk).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0002_m8_order_project"),
        ("projects", "0008_m8_expense_purchase"),
    ]

    operations = [
        migrations.RunPython(wrap_orphan_expenses, unwrap_orphan_expenses),
    ]
