"""Row-Level Security on ``audit_audit_log`` (T1.3) — the R4 backstop.

The ``AuditLog`` table was introduced additively at T1.2 (see
``apps.audit.models`` docstring) and is tenant-owned, so it needs the same RLS
backstop as every other tenant table. Enabled here with the shared M0 helper.

NOTE — scope boundary: this migration only adds **RLS**. The append-only
**immutability trigger** (forbid UPDATE/DELETE at the DB level) and auditing of
the remaining ``rbac.md`` §5 actions are **M5**'s job ("Finalize the
AuditLog"); the composite ``(tenant, entity_type, entity_id, created_at)`` index
already ships in ``0001_initial``. Nothing here blocks M5 layering the trigger
on top. Reverse drops the policy and disables RLS.
"""
from __future__ import annotations

from django.db import migrations

from apps.tenancy.db import disable_rls_sql, enable_rls_sql


class Migration(migrations.Migration):

    dependencies = [
        ("audit", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(
            sql=enable_rls_sql("audit_audit_log"),
            reverse_sql=disable_rls_sql("audit_audit_log"),
        ),
    ]
