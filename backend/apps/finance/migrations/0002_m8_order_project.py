"""M8 Phase 2 (revision) — an order belongs to exactly one project.

The first cut modelled `Payment` as tenant-wide, on the theory that one Amazon
checkout might pay for two grants at once. The user's actual rule is stricter:
an order is paid out of one project's funds and buys that project's assets,
full stop. Following it removes cross-project sharing, the redaction of other
projects' totals, and the separate top-level screen — all of which existed only
to serve a case that does not occur here.

Nullable, because the column is added to a table that already has rows in
development databases. Every order created through the API has a project (the
serializer requires it), so this is a migration-safety concession rather than a
meaningful state.

`CASCADE` matches `projects.Expense.project`: a project's costs go with it.
"""

from __future__ import annotations

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0001_initial"),
        ("projects", "0008_m8_expense_purchase"),
        ("tenancy", "0007_tenant_branding"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="payment",
            name="project",
            field=models.ForeignKey(
                blank=True,
                help_text="The project whose funds paid for this order. Nullable "
                "only so the column could be added to rows that predate it; every "
                "order created through the API has one (the serializer requires it).",
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="orders",
                to="projects.project",
            ),
        ),
        migrations.AddIndex(
            model_name="payment",
            index=models.Index(fields=["tenant", "project"], name="finance_pay_tenant__ce8e6a_idx"),
        ),
    ]
