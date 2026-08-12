"""M8 Phase 2 — seed the 2 new finance permission keys
(`finance.payment.view`, `finance.payment.manage`, added to
`apps.rbac.permission_keys` in this same change) and grant them to every
ALREADY-EXISTING tenant's system roles, per `docs/tasks/
M8-expense-reconciliation.md` §7:

| key                   | Admin | Project Lead | Member | Viewer |
|-----------------------|-------|--------------|--------|--------|
| finance.payment.view  | ✅    | 🟡           | ➖     | ➖     |
| finance.payment.manage| ✅    | 🟡           | ➖     | ➖     |

🟡 = granted, but only ever effective for a `Membership` scoped to the specific
project being acted on. **Which charges a lead may see at all** is a separate
question the permission system cannot express — a charge has no project — and
is answered by the visible-set rule in `apps.finance.permissions`: a lead sees
a charge carrying at least one line booked to a project they lead, with every
other project's lines collapsed into one unnamed total.

Same shape and the same reasoning as `0003_seed_project_grant_permissions` —
see that module for the full explanation of why both the forward and reverse
paths use HISTORICAL models (`apps.get_model`) rather than the concrete
classes, which describe today's schema rather than the schema in force when the
migration runs. That distinction caused real upgrade failures on both paths
once already; it is not theoretical.

**A new migration rather than an edit to `0003`**: `0003` has already run
everywhere, so editing it in place would never replay.

**Tenants created after this migration need no backfill** —
`apps.rbac.signals.seed_system_roles` calls the same helper at signal-fire time
and reads the updated `permission_keys.py`.
"""

from __future__ import annotations

from django.db import migrations

from ._helpers import unscoped

NEW_PERMISSION_KEYS = [
    "finance.payment.view",
    "finance.payment.manage",
]


def seed(apps, schema_editor):
    from apps.rbac.seed import seed_roles_for_tenant

    Permission = apps.get_model("rbac", "Permission")
    Role = apps.get_model("rbac", "Role")
    RolePermission = apps.get_model("rbac", "RolePermission")
    Tenant = apps.get_model("tenancy", "Tenant")
    using = schema_editor.connection.alias

    for tenant in unscoped(Tenant, using):
        seed_roles_for_tenant(
            tenant=tenant,
            role_model=Role,
            permission_model=Permission,
            role_permission_model=RolePermission,
            using=using,
        )


def unseed(apps, schema_editor):
    """Remove exactly what this migration could have added: the 2 keys, and
    their grants on SYSTEM roles only — a tenant's own custom roles are never
    touched."""
    Permission = apps.get_model("rbac", "Permission")
    RolePermission = apps.get_model("rbac", "RolePermission")
    using = schema_editor.connection.alias

    unscoped(RolePermission, using).filter(
        permission__key__in=NEW_PERMISSION_KEYS, role__is_system=True
    ).delete()
    unscoped(Permission, using).filter(key__in=NEW_PERMISSION_KEYS).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("rbac", "0005_rls_user_permission_override"),
        ("tenancy", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
