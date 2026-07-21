"""T4.5 — RLS (R4 backstop) on `jobs_job`, same house convention as every
other tenant-owned table's own migration (`CLAUDE.md`: "RLS via
`apps.tenancy.db.enable_rls_sql()` ... in its own migration, same milestone
as the table, non-negotiable"). Uses the shared `apps.tenancy.db.
enable_rls_sql` helper so the policy is byte-identical to every other
tenant table's — the app-level `TenantScopedManager` filter (T0.4) and the
DB policy can never drift. Fail-closed: no `app.current_tenant` GUC ->
`tenant_id = NULL` -> zero rows.
"""

from __future__ import annotations

from django.db import migrations

from apps.tenancy.db import disable_rls_sql, enable_rls_sql

TABLE = "jobs_job"


class Migration(migrations.Migration):

    dependencies = [
        ("jobs", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(sql=enable_rls_sql(TABLE), reverse_sql=disable_rls_sql(TABLE)),
    ]
