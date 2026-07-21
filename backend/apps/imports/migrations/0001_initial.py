# Generated for T6.1 (docs/tasks/M6-import-export-deploy.md) — mirrors
# `apps.jobs`'s `0001_initial.py` shape/conventions.

import apps.tenancy.managers
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("tenancy", "0004_rls_policies"),
        ("jobs", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ImportJob",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("dry_run_running", "Dry-run running"),
                            ("dry_run_succeeded", "Dry-run succeeded"),
                            ("dry_run_failed", "Dry-run failed"),
                            ("committing", "Committing"),
                            ("committed", "Committed"),
                            ("commit_failed", "Commit failed"),
                        ],
                        default="pending",
                        max_length=20,
                    ),
                ),
                ("source_filename", models.CharField(max_length=255)),
                ("source_storage_key", models.CharField(max_length=500)),
                ("source_content_type", models.CharField(blank=True, default="", max_length=127)),
                ("mapping", models.JSONField(blank=True, default=dict)),
                ("report", models.JSONField(blank=True, null=True)),
                ("created_asset_ids", models.JSONField(blank=True, default=list)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "commit_job",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="jobs.job",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="import_jobs",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "dry_run_job",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="jobs.job",
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
                "db_table": "imports_import_job",
                "ordering": ["-created_at"],
            },
            managers=[
                ("objects", apps.tenancy.managers.TenantScopedManager()),
            ],
        ),
        migrations.AddIndex(
            model_name="importjob",
            index=models.Index(fields=["tenant", "created_by"], name="imports_imp_tenant__ebfb72_idx"),
        ),
    ]
