"""RLS (R4 backstop) on `rbac_user_permission_override`, the tenant-owned
table added for the per-user permission overrides (docs/rbac.md §6).

Same house convention as every other tenant-owned table's own RLS migration
(CLAUDE.md: "Postgres RLS is the backstop on every tenant table"), using the
shared `apps.tenancy.db.enable_rls_sql` helper so the policy is
byte-identical to the rest and can never drift from the app-level
`TenantScopedManager` filter. Fail-closed: no `app.current_tenant` GUC ->
`tenant_id = NULL` -> zero rows.

This table is a particularly sharp case for the backstop: a row here is the
authorization decision itself, so a missed tenant filter would not just leak
data, it would let one tenant's override be read as another's.
"""

from __future__ import annotations

from django.db import migrations

from apps.tenancy.db import disable_rls_sql, enable_rls_sql

TABLE = "rbac_user_permission_override"


class Migration(migrations.Migration):

    dependencies = [
        ("rbac", "0004_role_is_customized_userpermissionoverride_and_more"),
    ]

    operations = [
        migrations.RunSQL(sql=enable_rls_sql(TABLE), reverse_sql=disable_rls_sql(TABLE)),
    ]
