"""M7 (T7.1) — RLS (R4 backstop) on every new project-hub tenant table.

Mirrors `apps.assets.migrations.0002_rls_indexes_search_vector`: each
tenant-owned table gets the byte-identical fail-closed tenant-isolation policy
via the shared `apps.tenancy.db.enable_rls_sql` helper, so the app-level
`TenantScopedManager` filter and the DB policy cannot drift. No
`app.current_tenant` GUC set -> zero rows.

`projects_project` ALREADY has RLS from `tenancy.0004_rls_policies` (it was a
tenant table before M7) — it is deliberately NOT in the list below, so this
migration neither re-enables nor re-creates its policy. The four tables here
are the ones M7 introduced (`0004_m7_project_hub_tables`).

The composite `(tenant, …)` btrees and the `(tenant, name)` unique constraint
are plain `models.Index`/`UniqueConstraint` the ORM already emitted in
`0004_m7_project_hub_tables`; this migration adds no specialized indexes (the
project hub needs none of the GIN/trigram/JSONB indexes the assets registry
does). Reversible + idempotent-safe (the helpers use `DROP POLICY IF EXISTS`).
"""
from __future__ import annotations

from django.db import migrations

from apps.tenancy.db import disable_rls_sql, enable_rls_sql

# New M7 tenant-owned tables ONLY. `projects_project` is intentionally excluded
# (its policy lives in tenancy.0004_rls_policies — do not re-add).
PROJECT_HUB_TENANT_TABLES = [
    "projects_expense_category",
    "projects_expense",
    "projects_project_document",
    "projects_expense_attachment",
]


class Migration(migrations.Migration):

    dependencies = [
        ("projects", "0004_m7_project_hub_tables"),
    ]

    operations = [
        *[
            migrations.RunSQL(
                sql=enable_rls_sql(table),
                reverse_sql=disable_rls_sql(table),
            )
            for table in PROJECT_HUB_TENANT_TABLES
        ],
    ]
