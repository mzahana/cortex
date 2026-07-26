"""M7 (T7.1) — additive grant metadata columns on `projects_project`.

Every column is nullable or defaulted, so this is safe on existing rows: a
project created before M7 simply carries empty funding metadata. `projects_project`
already has its RLS policy from `tenancy.0004_rls_policies`; adding columns does
not touch that policy, so nothing is re-added here (see `0005_m7_rls_indexes`).
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("projects", "0002_project_lead_user_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="project",
            name="code",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Optional short project/grant code, e.g. 'NSF-2026-014'.",
                max_length=64,
            ),
        ),
        migrations.AddField(
            model_name="project",
            name="funding_source",
            field=models.CharField(
                blank=True,
                choices=[("internal", "Internal"), ("external", "External")],
                default="",
                help_text="Internal budget vs. external grant (docs/data-model.md §2).",
                max_length=8,
            ),
        ),
        migrations.AddField(
            model_name="project",
            name="sponsor",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Awarding body / funding sponsor for an external grant.",
                max_length=255,
            ),
        ),
        migrations.AddField(
            model_name="project",
            name="start_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="project",
            name="end_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="project",
            name="budget_total",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Single awarded budget total; spend is broken down by "
                "ExpenseCategory in the report (no per-category allocations in M7).",
                max_digits=14,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="project",
            name="currency",
            field=models.CharField(blank=True, default="", max_length=3),
        ),
        migrations.AddField(
            model_name="project",
            name="status",
            field=models.CharField(
                choices=[("active", "Active"), ("closed", "Closed")],
                default="active",
                max_length=8,
            ),
        ),
        migrations.AddField(
            model_name="project",
            name="description",
            field=models.TextField(blank=True, default=""),
        ),
    ]
