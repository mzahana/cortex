"""M8 (Phase 1) — `ExpenseAssetLink`, replacing the single `Expense.asset` FK,
plus the backfill that moves every existing link into the new table.

See `docs/tasks/M8-expense-reconciliation.md` §1.5. RLS is created in the same
migration as the table, for the reason spelled out in
`apps.assets.migrations.0005_m8_asset_project_usage`.

**`Expense.asset` is NOT dropped here.** It stays as a deprecated, still-
populated column for one release so the frontend and
`test_expense_prefill_from_asset.py` can migrate without a flag day; the drop
is a separate, later migration (M8 §8). That also makes THIS migration cleanly
reversible: reversing the backfill just deletes the link rows, and no data has
been destroyed, because the source column is still sitting there.
"""

from __future__ import annotations

import django.db.models.deletion
from django.db import migrations, models

import apps.tenancy.managers
from apps.tenancy.db import disable_rls_sql, enable_rls_sql


def backfill_asset_links(apps_registry, schema_editor):
    """Copy every `Expense.asset` into one `ExpenseAssetLink` row.

    Runs as the migration (table-owner) role, which bypasses RLS by ownership,
    so this correctly walks every tenant's rows in one pass — the same reason
    `apps.projects.migrations.0006_seed_expense_categories` can seed per-tenant
    without entering `tenant_context`. `tenant_id` is copied from the expense
    itself, never inferred, so each link lands in exactly the tenant its parent
    expense belongs to.

    **`_base_manager`, not `objects` and not `all_objects`.** The historical
    model rendered by `apps_registry` carries only managers with
    `use_in_migrations = True`, which here is exactly one: the fail-closed
    `TenantScopedManager` bound to `objects`. Calling it outside
    `tenant_context` raises `TenantContextError` (correctly — that is the R4
    guard doing its job), and `all_objects` simply does not exist on a
    historical model. `_base_manager` is Django's always-present unscoped
    manager and is the right tool for a cross-tenant data migration.
    (`0006_seed_expense_categories` gets away with `all_objects` only because
    it imports the CONCRETE model instead of the historical one — deliberate
    there for a seed, but wrong for a backfill, which must replay against the
    schema as it stood at THIS point in history, not as it stands today.)

    `allocated_amount` is set to the line's full `amount`: before M8 one
    expense meant one asset, so that asset carried 100% of the line's cost.
    Bulk-created in batches to stay memory-flat on a large ledger.
    """
    Expense = apps_registry.get_model("projects", "Expense")
    ExpenseAssetLink = apps_registry.get_model("projects", "ExpenseAssetLink")

    rows = (
        Expense._base_manager.filter(asset__isnull=False)
        .values("id", "tenant_id", "asset_id", "amount")
        .iterator(chunk_size=1000)
    )
    batch: list = []
    for row in rows:
        batch.append(
            ExpenseAssetLink(
                tenant_id=row["tenant_id"],
                expense_id=row["id"],
                asset_id=row["asset_id"],
                allocated_amount=row["amount"],
                quantity=1,
            )
        )
        if len(batch) >= 1000:
            ExpenseAssetLink._base_manager.bulk_create(batch)
            batch = []
    if batch:
        ExpenseAssetLink._base_manager.bulk_create(batch)


def unbackfill_asset_links(apps_registry, schema_editor):
    """Reverse: drop every link row. Non-destructive — `Expense.asset` still
    holds the original value (see this module's docstring)."""
    ExpenseAssetLink = apps_registry.get_model("projects", "ExpenseAssetLink")
    ExpenseAssetLink._base_manager.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("assets", "0005_m8_asset_project_usage"),
        ("projects", "0006_seed_expense_categories"),
        ("tenancy", "0007_tenant_branding"),
    ]

    operations = [
        migrations.CreateModel(
            name="ExpenseAssetLink",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "allocated_amount",
                    models.DecimalField(
                        blank=True,
                        decimal_places=2,
                        help_text="This asset's share of the expense line, in the "
                        "line's currency. NULL = informational link with no cost split.",
                        max_digits=14,
                        null=True,
                    ),
                ),
                ("quantity", models.PositiveIntegerField(default=1)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "asset",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="expense_links",
                        to="assets.asset",
                    ),
                ),
                (
                    "expense",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="asset_links",
                        to="projects.expense",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="tenancy.tenant",
                    ),
                ),
            ],
            options={
                "db_table": "projects_expense_asset_link",
                "ordering": ["id"],
                "indexes": [
                    models.Index(
                        fields=["tenant", "expense"], name="projects_ex_tenant__afeda9_idx"
                    ),
                    models.Index(
                        fields=["tenant", "asset"], name="projects_ex_tenant__1a2476_idx"
                    ),
                ],
            },
            managers=[
                ("objects", apps.tenancy.managers.TenantScopedManager()),
            ],
        ),
        migrations.AddConstraint(
            model_name="expenseassetlink",
            constraint=models.UniqueConstraint(
                fields=("tenant", "expense", "asset"), name="uniq_expense_asset_link"
            ),
        ),
        migrations.RunSQL(
            sql=enable_rls_sql("projects_expense_asset_link"),
            reverse_sql=disable_rls_sql("projects_expense_asset_link"),
        ),
        migrations.RunPython(backfill_asset_links, unbackfill_asset_links),
    ]
