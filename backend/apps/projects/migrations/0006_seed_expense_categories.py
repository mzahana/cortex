"""M7 (T7.1) — seed the default `ExpenseCategory` set for existing tenants.

Follows the `apps.rbac.migrations.0002_seed_permissions_and_roles` pattern: a
`RunPython` data migration that imports the concrete model (safe here — it
necessarily runs after the `0004`/`0005` schema it targets) and is
idempotent (`get_or_create`) so it is safe to re-run in CI.

**Tenant-aware:** seeds the starter categories once per already-existing
`Tenant`, writing through `all_objects` with an explicit `tenant=` (the
default `TenantScopedManager` is fail-closed and has no request/task tenant
context during a migration — migrations run as the table owner and bypass RLS,
so a cross-tenant seed loop is correct here). On a fresh, tenant-less DB this
is a harmless no-op; per-tenant seeding for tenants created *after* this
migration is left to the M7 backend slice (a `Tenant` post-save signal,
mirroring `apps.rbac.signals.seed_system_roles`) — flagged for
backend-engineer.

**Reversible:** the down path deletes only rows whose name is in the seeded
set (`is_system`-style scoping isn't available on this simple config model, so
name-matching is the accepted trade-off, exactly as `rbac.unseed` deletes by
key); a tenant's own custom categories with other names are untouched.
"""
from __future__ import annotations

from django.db import migrations

# docs/tasks/M7-project-grants.md / data-model.md §2 starter set.
DEFAULT_CATEGORIES = [
    "Equipment",
    "Consumables",
    "Services",
    "Software",
    "Travel",
    "Shipping",
    "Other",
]


def seed(apps, schema_editor):
    from apps.projects.models import ExpenseCategory
    from apps.tenancy.models import Tenant

    for tenant in Tenant.objects.all():
        for name in DEFAULT_CATEGORIES:
            ExpenseCategory.all_objects.get_or_create(
                tenant=tenant,
                name=name,
                defaults={"is_active": True},
            )


def unseed(apps, schema_editor):
    from apps.projects.models import ExpenseCategory

    ExpenseCategory.all_objects.filter(name__in=DEFAULT_CATEGORIES).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("projects", "0005_m7_rls_indexes"),
        ("tenancy", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed, reverse_code=unseed),
    ]
