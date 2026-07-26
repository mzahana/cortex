"""M7 (T7.1) — the project-hub tables: `ExpenseCategory`, `Expense`,
`ProjectDocument`, `ExpenseAttachment`.

Plain `CreateModel`s (Django emits the composite `(tenant, …)` btrees and the
unique constraint the ORM can express). The fail-closed RLS policy on each new
tenant-owned table is added separately in `0005_m7_rls_indexes` (byte-identical
to every other tenant table via `apps.tenancy.db.enable_rls_sql`), keeping the
schema and the security backstop as distinct, independently-reversible steps —
same split the assets app uses (`0001_initial` + `0002_rls_indexes_...`).
"""

import apps.tenancy.managers
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("assets", "0002_rls_indexes_search_vector"),
        ("projects", "0003_m7_project_fields"),
        ("tenancy", "0004_rls_policies"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ExpenseCategory",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=255)),
                ("is_active", models.BooleanField(default=True)),
                ("tenant", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="+", to="tenancy.tenant")),
            ],
            options={
                "db_table": "projects_expense_category",
                "ordering": ["name"],
            },
            managers=[
                ("objects", apps.tenancy.managers.TenantScopedManager()),
            ],
        ),
        migrations.CreateModel(
            name="Expense",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("amount", models.DecimalField(decimal_places=2, max_digits=14)),
                ("currency", models.CharField(blank=True, default="", max_length=3)),
                ("date", models.DateField()),
                ("vendor", models.CharField(blank=True, default="", max_length=255)),
                ("invoice_number", models.CharField(blank=True, default="", max_length=128)),
                ("description", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("asset", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="expenses", to="assets.asset")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_expenses", to=settings.AUTH_USER_MODEL)),
                ("category", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="expenses", to="projects.expensecategory")),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="expenses", to="projects.project")),
                ("tenant", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="+", to="tenancy.tenant")),
            ],
            options={
                "db_table": "projects_expense",
                "ordering": ["-date", "-id"],
            },
            managers=[
                ("objects", apps.tenancy.managers.TenantScopedManager()),
            ],
        ),
        migrations.CreateModel(
            name="ProjectDocument",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("kind", models.CharField(choices=[("proposal", "Proposal"), ("contract", "Contract"), ("progress_report", "Progress report"), ("other", "Other")], default="other", max_length=20)),
                ("storage_key", models.CharField(help_text="Relative path/key on the storage backend (volume or, later, S3-compatible object storage) — never the binary itself.", max_length=500)),
                ("filename", models.CharField(max_length=255)),
                ("content_type", models.CharField(blank=True, default="", max_length=127)),
                ("size", models.PositiveBigIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="documents", to="projects.project")),
                ("tenant", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="+", to="tenancy.tenant")),
                ("uploaded_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="uploaded_project_documents", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "db_table": "projects_project_document",
                "ordering": ["-created_at"],
            },
            managers=[
                ("objects", apps.tenancy.managers.TenantScopedManager()),
            ],
        ),
        migrations.CreateModel(
            name="ExpenseAttachment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("storage_key", models.CharField(help_text="Relative path/key on the storage backend (volume or, later, S3-compatible object storage) — never the binary itself.", max_length=500)),
                ("filename", models.CharField(max_length=255)),
                ("content_type", models.CharField(blank=True, default="", max_length=127)),
                ("size", models.PositiveBigIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("expense", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="attachments", to="projects.expense")),
                ("tenant", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="+", to="tenancy.tenant")),
                ("uploaded_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="uploaded_expense_attachments", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "db_table": "projects_expense_attachment",
                "ordering": ["-created_at"],
                "indexes": [models.Index(fields=["tenant", "expense"], name="projects_ex_tenant__1b65e5_idx")],
            },
            managers=[
                ("objects", apps.tenancy.managers.TenantScopedManager()),
            ],
        ),
        migrations.AddConstraint(
            model_name="expensecategory",
            constraint=models.UniqueConstraint(fields=("tenant", "name"), name="uniq_expense_category_tenant_name"),
        ),
        migrations.AddIndex(
            model_name="expense",
            index=models.Index(fields=["tenant", "project"], name="projects_ex_tenant__d44977_idx"),
        ),
        migrations.AddIndex(
            model_name="expense",
            index=models.Index(fields=["tenant", "category"], name="projects_ex_tenant__9a498f_idx"),
        ),
        migrations.AddIndex(
            model_name="expense",
            index=models.Index(fields=["tenant", "date"], name="projects_ex_tenant__57470c_idx"),
        ),
        migrations.AddIndex(
            model_name="projectdocument",
            index=models.Index(fields=["tenant", "project"], name="projects_pr_tenant__8efe19_idx"),
        ),
    ]
