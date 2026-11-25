"""Counted assets — `Asset.tracking` + `Asset.quantity` and their DB invariants.

A *counted* asset is one record standing for N identical durable units
(screwdrivers, cables); an *individual* asset is one unit (every existing row).
`quantity` is NOT the consumable stock count — consumables keep
`StockItem.quantity_on_hand`, and `asset_consumable_not_counted` keeps the two
from ever meeting on one row.

**Cheap on a large table.** Both `AddField`s use a constant default, which
Postgres 11+ applies as a catalog-only change (no table rewrite); Django then
drops the column DEFAULT, also catalog-only. Existing rows therefore read as
`tracking='individual', quantity=1`, which satisfies every new constraint. The
four `CHECK`s each do one validating scan under the `ALTER TABLE` lock — linear,
no rewrite, negligible at this table's size (tens of thousands of rows at most).

**No new RLS.** `assets_asset` already has the shared tenant policy from
`0002_rls_indexes_search_vector`; Postgres RLS policies are row-level, so new
columns on the table are covered automatically.

**No `(tenant, tracking)` index** — see the comment in `Asset.Meta.indexes`.

Reversible: the reverse drops the four constraints then the two columns.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("assets", "0005_m8_asset_project_usage"),
    ]

    operations = [
        migrations.AddField(
            model_name="asset",
            name="quantity",
            field=models.PositiveIntegerField(
                default=1,
                help_text="Number of identical units for a counted asset; always 1 for an "
                "individual asset (DB-enforced). NOT the consumable stock count — that is "
                "`StockItem.quantity_on_hand`.",
            ),
        ),
        migrations.AddField(
            model_name="asset",
            name="tracking",
            field=models.CharField(
                choices=[("individual", "Individual"), ("counted", "Counted")],
                default="individual",
                help_text="'individual' = this record is one physical unit; 'counted' = "
                "this record stands for `quantity` identical durable units. Consumables "
                "are never counted (they use StockItem instead).",
                max_length=16,
            ),
        ),
        migrations.AddConstraint(
            model_name="asset",
            constraint=models.CheckConstraint(
                check=models.Q(("tracking__in", ["individual", "counted"])),
                name="asset_tracking_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="asset",
            constraint=models.CheckConstraint(
                check=models.Q(("quantity__gte", 1)), name="asset_quantity_positive"
            ),
        ),
        migrations.AddConstraint(
            model_name="asset",
            constraint=models.CheckConstraint(
                check=models.Q(("tracking", "counted"), ("quantity", 1), _connector="OR"),
                name="asset_individual_quantity_one",
            ),
        ),
        migrations.AddConstraint(
            model_name="asset",
            constraint=models.CheckConstraint(
                check=models.Q(("is_consumable", True), ("tracking", "counted"), _negated=True),
                name="asset_consumable_not_counted",
            ),
        ),
    ]
