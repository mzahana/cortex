"""M8 (Phase 1) — `AssetProjectUsage`: which projects *use* an asset, as
distinct from `Asset.project`, which records who *funded* it.

See `docs/tasks/M8-expense-reconciliation.md` §1.6 and the model docstring for
why the two are separate concepts (short version: conflating them either loses
the funding record when usage changes, or implies one asset's cost belongs to
several grants at once — an audit finding).

**RLS is created in this same migration, not a follow-up one.** M7 split
"create tables" and "add RLS" across `0004_m7_project_hub_tables` /
`0005_m7_rls_indexes`; that leaves a window — however brief, and however
migration-time-only — in which a tenant-owned table exists with no fail-closed
policy on it. Since the R4 backstop is the single highest-stakes invariant in
this codebase (CLAUDE.md), the policy goes on in the same atomic migration that
creates the table. The SQL still comes from the shared `apps.tenancy.db`
helpers, so it is byte-identical to every other tenant table's policy and
cannot drift.

Reversible: the constraints/table drop as usual, and `disable_rls_sql` uses
`DROP POLICY IF EXISTS`, so a down-migration is safe and re-runnable.
"""

from __future__ import annotations

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import apps.tenancy.managers
from apps.tenancy.db import disable_rls_sql, enable_rls_sql


class Migration(migrations.Migration):

    dependencies = [
        ("assets", "0004_attachment_doc_type"),
        ("projects", "0006_seed_expense_categories"),
        ("tenancy", "0007_tenant_branding"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="AssetProjectUsage",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("start_date", models.DateField(blank=True, null=True)),
                (
                    "end_date",
                    models.DateField(
                        blank=True, help_text="NULL = usage is ongoing.", null=True
                    ),
                ),
                ("note", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "asset",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="project_usages",
                        to="assets.asset",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="created_asset_usages",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="asset_usages",
                        to="projects.project",
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
                "db_table": "assets_asset_project_usage",
                "ordering": ["-start_date", "-id"],
                "indexes": [
                    models.Index(fields=["tenant", "asset"], name="assets_asse_tenant__b3c54f_idx"),
                    models.Index(
                        fields=["tenant", "project"], name="assets_asse_tenant__52892f_idx"
                    ),
                ],
            },
            managers=[
                ("objects", apps.tenancy.managers.TenantScopedManager()),
            ],
        ),
        # At most one OPEN-ENDED usage per (asset, project) — an asset can be
        # used by the same project across several distinct historical periods,
        # but cannot be "currently in use" by it twice.
        migrations.AddConstraint(
            model_name="assetprojectusage",
            constraint=models.UniqueConstraint(
                condition=models.Q(("end_date__isnull", True)),
                fields=("tenant", "asset", "project"),
                name="uniq_open_asset_project_usage",
            ),
        ),
        migrations.AddConstraint(
            model_name="assetprojectusage",
            constraint=models.CheckConstraint(
                check=models.Q(
                    ("end_date__isnull", True),
                    ("start_date__isnull", True),
                    ("end_date__gte", models.F("start_date")),
                    _connector="OR",
                ),
                name="ck_asset_usage_dates_ordered",
            ),
        ),
        migrations.RunSQL(
            sql=enable_rls_sql("assets_asset_project_usage"),
            reverse_sql=disable_rls_sql("assets_asset_project_usage"),
        ),
    ]
