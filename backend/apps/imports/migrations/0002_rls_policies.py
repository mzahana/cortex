"""T6.1 — RLS (R4 backstop) on `imports_import_job`, same house convention
as every other tenant-owned table's own migration (`CLAUDE.md`: "RLS via
`apps.tenancy.db.enable_rls_sql()` ... in its own migration, same milestone
as the table, non-negotiable"). Mirrors `apps.jobs`'s `0002_rls_policies.py`
byte-for-byte in structure.
"""

from __future__ import annotations

from django.db import migrations

from apps.tenancy.db import disable_rls_sql, enable_rls_sql

TABLE = "imports_import_job"


class Migration(migrations.Migration):

    dependencies = [
        ("imports", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(sql=enable_rls_sql(TABLE), reverse_sql=disable_rls_sql(TABLE)),
    ]
