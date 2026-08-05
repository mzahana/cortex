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

from ._helpers import unscoped

NEW_PERMISSION_KEYS = [
    "project.view",
    "project.manage",
    "expense.view",
    "expense.manage",
]


def seed(apps, schema_editor):
    # HISTORICAL models here too, NOT `apps.rbac.models` — for the same class
    # of reason as `unseed` below, but on the FORWARD path, and it broke real
    # upgrades rather than just the CI reverse gate:
    #
    #   `seed_roles_for_tenant` does a `get_or_create` on `Role`, and the
    #   concrete `Role` has carried `is_customized` since `rbac.0004` — one
    #   migration LATER than this one. So on any database with tenant rows,
    #   forward `migrate` died right here with
    #       ProgrammingError: column rbac_role.is_customized does not exist
    #   The `migrate up -> down -> up` CI gate never caught it because CI's
    #   database has no tenants, so this loop body never executed.
    #
    # The historical `Role` from `apps.get_model` describes the schema as of
    # this migration (no `is_customized`, and `seed.py` tolerates its absence
    # via `getattr`), so the SELECT only names columns that exist.
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
    # HISTORICAL models here, deliberately — NOT `apps.rbac.models`, which the
    # forward `seed()` above uses.
    #
    # A REVERSE runs against an OLDER schema than the one today's concrete
    # model classes describe, and the ORM emits SQL from the class it is
    # handed. Two ways that bit here, both real CI failures:
    #
    # 1. Cascades. `.delete()` runs the deletion collector over the model's
    #    reverse relations. Concrete `Permission` has carried
    #    `UserPermissionOverride.permission` (on_delete=CASCADE) since
    #    `0004_role_is_customized_userpermissionoverride_and_more`, so the
    #    collector queried `rbac_user_permission_override` — a table 0004's
    #    own reverse had already dropped:
    #        ProgrammingError: relation "rbac_user_permission_override"
    #        does not exist
    # 2. Columns. Concrete `Role` has `is_customized`, also added by 0004, so
    #    the collector's SELECT named a column that no longer exists:
    #        ProgrammingError: column rbac_role.is_customized does not exist
    #
    # The migration graph is NOT at fault: 0004 depends on 0003, so Django
    # unapplies 0005 -> 0004 -> 0003, which is the only correct order (0004's
    # table has an FK to the very `Permission` rows 0003 seeds). The bug was
    # this reverse code describing a schema that is gone by the time it runs.
    #
    # The historical models come from `from_state` (the state *before* 0003,
    # i.e. after 0002), so they know only the tables/columns that still exist
    # at this point — no `is_customized`, no `UserPermissionOverride`. Nothing
    # is orphaned by this: 0003 can never be unapplied while 0004 is still
    # applied, so any override row referencing these permissions is already
    # gone with its table.
    Permission = apps.get_model("rbac", "Permission")
    RolePermission = apps.get_model("rbac", "RolePermission")
    using = schema_editor.connection.alias

    unscoped(RolePermission, using).filter(
        permission__key__in=NEW_PERMISSION_KEYS, role__is_system=True
    ).delete()
    unscoped(Permission, using).filter(key__in=NEW_PERMISSION_KEYS).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("rbac", "0002_seed_permissions_and_roles"),
        ("tenancy", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed, reverse_code=unseed),
    ]
