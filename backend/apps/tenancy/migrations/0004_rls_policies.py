"""Row-Level Security on every tenant-owned table (T0.5) — the R4 backstop.

Enables RLS + a single ``tenant_isolation`` policy on each tenant-owned table.
The policy (see ``apps.tenancy.db.RLS_PREDICATE``) restricts every row to the
tenant currently in the ``app.current_tenant`` session GUC, and is **fail-closed**:
with no tenant set the predicate is ``tenant_id = NULL`` -> zero rows. It applies
to SELECT/INSERT/UPDATE/DELETE (``USING`` + ``WITH CHECK``), so a role subject
to RLS can neither read nor write another tenant's rows.

## Tables covered (the full tenant-owned set at M0)

- ``accounts_user``           (accounts.User)
- ``rbac_role``               (rbac.Role)
- ``rbac_role_permission``    (rbac.RolePermission — carries denormalized tenant_id)
- ``rbac_membership``         (rbac.Membership)
- ``projects_project``        (projects.Project)

Deliberately **not** covered (not tenant-owned):
- ``tenancy_tenant``  — the root of the tenant tree; nothing sits "above" it.
  Leaving it un-RLS'd is also what lets the T0.6 login path resolve a tenant by
  slug *before* any GUC/session exists (see ``apps.tenancy.db``).
- ``rbac_permission`` — a fixed, system-wide vocabulary shared by all tenants.

Owner/superuser roles (migrations, seed, management commands) bypass RLS by
ownership, so this does not disturb the T0.4 seed migration. Enforcement bites
for the non-superuser ``cortex_app`` runtime role provisioned in ``0003``.

Reverse drops each policy and disables RLS.
"""
from __future__ import annotations

from django.db import migrations

from apps.tenancy.db import disable_rls_sql, enable_rls_sql

# The full tenant-owned table set as of M0. Future tenant tables (M1+ assets,
# categories, locations, ...) add themselves in their own migrations using the
# same `enable_rls_sql` helper.
TENANT_TABLES = [
    "accounts_user",
    "rbac_role",
    "rbac_role_permission",
    "rbac_membership",
    "projects_project",
]


class Migration(migrations.Migration):

    dependencies = [
        ("tenancy", "0003_app_db_role"),
        ("accounts", "0001_initial"),
        ("projects", "0001_initial"),
        ("rbac", "0002_seed_permissions_and_roles"),
    ]

    operations = [
        migrations.RunSQL(
            sql=enable_rls_sql(table),
            reverse_sql=disable_rls_sql(table),
        )
        for table in TENANT_TABLES
    ]
