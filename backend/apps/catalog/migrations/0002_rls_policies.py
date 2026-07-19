"""Row-Level Security on the M1 catalog tables (T1.3) — the R4 backstop.

Enables RLS + the single shared ``tenant_isolation`` policy (see
``apps.tenancy.db.enable_rls_sql`` / ``RLS_PREDICATE``) on every tenant-owned
table introduced by T1.1. Reuses the identical, reviewed M0 helper rather than
re-typing the predicate so the app-level ``TenantScopedManager`` filter and the
DB policy can never drift.

Tables covered (the full T1.1 tenant-owned set):
- ``catalog_category``          (catalog.Category)
- ``catalog_custom_field_def``  (catalog.CustomFieldDef)
- ``catalog_location``          (catalog.Location)
- ``catalog_tag``               (catalog.Tag)

Fail-closed: with no ``app.current_tenant`` GUC set the predicate is
``tenant_id = NULL`` -> zero rows. Enforcement bites for the non-superuser
``cortex_app`` runtime role; the owner (migrations/seed) bypasses by ownership.
Reverse drops each policy and disables RLS.
"""
from __future__ import annotations

from django.db import migrations

from apps.tenancy.db import disable_rls_sql, enable_rls_sql

CATALOG_TENANT_TABLES = [
    "catalog_category",
    "catalog_custom_field_def",
    "catalog_location",
    "catalog_tag",
]


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(
            sql=enable_rls_sql(table),
            reverse_sql=disable_rls_sql(table),
        )
        for table in CATALOG_TENANT_TABLES
    ]
