"""Seed data migration (T0.4): the full permission-key set from
`docs/rbac.md` §3, plus the 4 system roles + grants for any tenants that
already exist at migrate-time.

Idempotent (`apps.rbac.seed` uses `get_or_create` throughout) — safe to run
more than once, and safe on a fresh (tenant-less) database, where it seeds
only the global `Permission` rows and leaves per-tenant role seeding to
`apps.rbac.signals.seed_system_roles` (fired on every future `Tenant`
creation).

NOTE for db-migration-specialist (T0.5): this migration imports the concrete
model classes directly rather than `apps.get_model(...)` historical models,
because the seed helpers rely on manager methods (`all_objects`,
`get_or_create`) that historical models won't carry reliably. This is the
standard accepted trade-off for a seed migration that runs once, immediately
after the schema it targets — it is not meant to be replayed against a
materially different future schema. Flag if this needs to change once RLS is
in place (RLS should not block this migration, since migrations run as the
table owner / bypass RLS by default).
"""
from __future__ import annotations

from django.db import migrations


def seed(apps, schema_editor):
    # Deferred import: safe here because this migration necessarily runs
    # after both apps' initial migrations (see `dependencies` below), so the
    # real model classes already match the DB schema at this point.
    from apps.rbac.models import Permission, Role, RolePermission
    from apps.rbac.seed import seed_permissions, seed_roles_for_tenant
    from apps.tenancy.models import Tenant

    seed_permissions(Permission)

    # `.only("id")` is load-bearing, not an optimization: this migration uses
    # the CONCRETE `Tenant` model (see the module docstring), so a plain
    # `Tenant.objects.all()` would `SELECT` every column the model has *today*
    # — including any added by a LATER `tenancy` migration that, on a fresh
    # database, has not run yet (this really broke `migrate` from scratch when
    # `tenancy.0007` added the branding columns). Selecting only the primary
    # key keeps this seed independent of the table's future shape, while still
    # yielding real `Tenant` instances (`seed_roles_for_tenant` assigns them to
    # a concrete `Role.tenant` FK, which a historical model instance can't
    # satisfy).
    for tenant in Tenant.objects.only("id"):
        seed_roles_for_tenant(
            tenant=tenant,
            role_model=Role,
            permission_model=Permission,
            role_permission_model=RolePermission,
        )


def unseed(apps, schema_editor):
    # Reversible: drop every row this migration could have created. Custom
    # tenant-authored roles/grants (is_system=False) are left untouched.
    from apps.rbac.models import Permission, Role, RolePermission
    from apps.rbac.permission_keys import PERMISSION_LABELS

    RolePermission.all_objects.filter(role__is_system=True).delete()
    Role.all_objects.filter(is_system=True).delete()
    Permission.objects.filter(key__in=PERMISSION_LABELS.keys()).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("rbac", "0001_initial"),
        ("tenancy", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed, reverse_code=unseed),
    ]
