"""M8 Phase 2 — `Expense.purchase`: which vendor receipt a line appeared on.

Additive and nullable, so every M7-era expense stays valid: `purchase IS NULL`
means "a standalone cost with no receipt recorded", which is a normal,
permanent state (a cash expense, a bank fee, anything never itemized), not a
migration gap to be backfilled later.

`SET_NULL` rather than `CASCADE`: deleting a mis-entered receipt must never
delete the financial records booked against it. The lines survive, unlinked,
and the reconciliation view surfaces them as unattached — which is recoverable,
whereas a cascade is not.
"""

from __future__ import annotations

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("assets", "0005_m8_asset_project_usage"),
        ("finance", "0001_initial"),
        ("projects", "0007_m8_expense_asset_link"),
        ("tenancy", "0007_tenant_branding"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="expense",
            name="purchase",
            field=models.ForeignKey(
                blank=True,
                help_text="The vendor receipt this line appeared on (M8 Phase 2). "
                "NULL = a standalone expense with no receipt recorded — every "
                "M7-era row, and still perfectly valid.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="expenses",
                to="finance.purchase",
            ),
        ),
        migrations.AddIndex(
            model_name="expense",
            index=models.Index(
                fields=["tenant", "purchase"], name="projects_ex_tenant__9d1e72_idx"
            ),
        ),
    ]
