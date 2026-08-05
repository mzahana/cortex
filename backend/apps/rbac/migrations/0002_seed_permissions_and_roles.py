"""Seed data migration (T0.4): the full permission-key set from
`docs/rbac.md` §3, plus the 4 system roles + grants for any tenants that
already exist at migrate-time.

Idempotent (`apps.rbac.seed` uses `get_or_create` throughout) — safe to run
more than once, and safe on a fresh (tenant-less) database, where it seeds
only the global `Permission` rows and leaves per-tenant role seeding to
`apps.rbac.signals.seed_system_roles` (fired on every future `Tenant`
creation).

HISTORICAL MODELS, both directions. An earlier revision of this migration
imported the CONCRETE model classes (on the theory that the seed helpers
needed the `all_objects` manager, which isn't serialized into migration
state). That was wrong in both directions and broke real migrate runs — see
the long note in `0003`'s `unseed` for the reverse side, and `0003`'s `seed`
for the forward side (`column rbac_role.is_customized does not exist`, a
column added a migration LATER than the seed that selected it). The seed
helpers now take a plain unfiltered queryset via `apps.rbac.seed.unscoped`,
so historical models work everywhere.

RLS does not block this migration: migrations run as the table owner, which
bypasses RLS by default.
"""
from __future__ import annotations

from django.db import migrations

from ._helpers import unscoped


def seed(apps, schema_editor):
    from apps.rbac.seed import seed_permissions, seed_roles_for_tenant

    Permission = apps.get_model("rbac", "Permission")
    Role = apps.get_model("rbac", "Role")
    RolePermission = apps.get_model("rbac", "RolePermission")
    Tenant = apps.get_model("tenancy", "Tenant")
    using = schema_editor.connection.alias

    seed_permissions(Permission, using)

    # Historical `Tenant`, so this iterates only the columns that exist at
    # THIS point in the migration graph — no `.only("id")` guard needed
    # against columns a later `tenancy` migration adds (`tenancy.0007`'s
    # branding columns really did break `migrate` from scratch back when this
    # used the concrete class). The historical instances are assignable to
    # historical `Role.tenant`, which is what the seed helper now writes.
    for tenant in unscoped(Tenant, using):
        seed_roles_for_tenant(
            tenant=tenant,
            role_model=Role,
            permission_model=Permission,
            role_permission_model=RolePermission,
            using=using,
        )


def unseed(apps, schema_editor):
    # Reversible: drop every row this migration could have created. Custom
    # tenant-authored roles/grants (is_system=False) are left untouched.
    #
    # Uses HISTORICAL models (`apps.get_model`), NOT the concrete classes the
    # forward `seed()` above imports — see the long note in `0003`'s `unseed`
    # for the full reasoning. Short version: a reverse runs against an OLDER
    # schema than the one today's model classes describe, so the concrete
    # classes name columns and cascade to tables that no longer exist by then.
    from apps.rbac.permission_keys import PERMISSION_LABELS

    Permission = apps.get_model("rbac", "Permission")
    Role = apps.get_model("rbac", "Role")
    RolePermission = apps.get_model("rbac", "RolePermission")
    using = schema_editor.connection.alias

    unscoped(RolePermission, using).filter(role__is_system=True).delete()
    unscoped(Role, using).filter(is_system=True).delete()
    unscoped(Permission, using).filter(key__in=PERMISSION_LABELS.keys()).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("rbac", "0001_initial"),
        ("tenancy", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed, reverse_code=unseed),
    ]
