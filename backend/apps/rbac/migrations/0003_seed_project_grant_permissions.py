"""M7 (Project & Grant Management, docs/tasks/M7-project-grants.md) — seed the
4 new permission keys (`project.view`, `project.manage`, `expense.view`,
`expense.manage`, added to `apps.rbac.permission_keys` in this same change)
and grant them to every ALREADY-EXISTING tenant's system roles, per the
matrix in `docs/rbac.md` §3 additions:

| key             | Admin | Project Lead | Member | Viewer |
|-----------------|-------|--------------|--------|--------|
| project.view    | ✅    | 🟡           | ✅     | ✅     |
| project.manage  | ✅    | 🟡           | ➖     | ➖     |
| expense.view    | ✅    | 🟡           | ➖     | ➖     |
| expense.manage  | ✅    | 🟡           | ➖     | ➖     |

**Reuses `apps.rbac.seed.seed_roles_for_tenant` unchanged** (single source of
truth: `SYSTEM_ROLE_PERMISSIONS`/`PERMISSION_LABELS` in `permission_keys.py`
already include the 4 new keys after this change, so replaying the exact same
seed helper `0002` used simply fills in the gaps via its existing
`get_or_create` calls — no separate literal copy of the matrix to drift from
that module). Runs for every tenant, not just ones missing the new grants:
harmless no-op for keys a tenant already has.

**Deliberately a NEW migration, not an edit to the historical
`0002_seed_permissions_and_roles`** (task instruction) — `0002` already ran
against production-shaped data; editing it in place wouldn't replay for a
tenant whose migrations table already marks it applied.

**Tenants created AFTER this migration need no separate backfill**: `apps.
rbac.signals.seed_system_roles` (fired on every `Tenant` post_save) calls the
SAME `seed_roles_for_tenant` helper at signal-fire time, which already reads
the updated `permission_keys.py` — this migration only backfills tenants that
predate the code change.

**Reversible:** the down path removes exactly the grants this migration could
have added (the 4 new keys, system roles only) and the 4 `Permission` rows —
never touches a tenant's own custom (non-system) roles/grants.
"""

from __future__ import annotations

from django.db import migrations

NEW_PERMISSION_KEYS = [
    "project.view",
    "project.manage",
    "expense.view",
    "expense.manage",
]


def seed(apps, schema_editor):
    from apps.rbac.models import Permission, Role, RolePermission
    from apps.rbac.seed import seed_roles_for_tenant
    from apps.tenancy.models import Tenant

    for tenant in Tenant.objects.all():
        seed_roles_for_tenant(
            tenant=tenant,
            role_model=Role,
            permission_model=Permission,
            role_permission_model=RolePermission,
        )


def unseed(apps, schema_editor):
    from apps.rbac.models import Permission, RolePermission

    RolePermission.all_objects.filter(
        permission__key__in=NEW_PERMISSION_KEYS, role__is_system=True
    ).delete()
    Permission.objects.filter(key__in=NEW_PERMISSION_KEYS).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("rbac", "0002_seed_permissions_and_roles"),
        ("tenancy", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed, reverse_code=unseed),
    ]
