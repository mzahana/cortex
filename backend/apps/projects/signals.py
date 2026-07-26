"""Auto-seeding signal (M7, T7.1 carried gap — `docs/tasks/M7-project-grants.md`).

`0006_seed_expense_categories` (the data migration) seeded the starter
`ExpenseCategory` set for every tenant that ALREADY EXISTED at migrate time,
same one-time-backfill pattern as `apps.rbac.migrations.
0002_seed_permissions_and_roles`. That migration's own docstring flagged the
gap this module closes: a tenant created AFTER that migration ran gets NO
`ExpenseCategory` rows at all (nothing else seeds them going forward).

Mirrors `apps.rbac.signals.seed_system_roles` exactly: hooked to `Tenant`
`post_save` (`created=True` only), entered inside `tenant_context(instance.id)`
before writing — this handler fires from INSIDE `Tenant.objects.
get_or_create(...)`, before the caller could have entered that context
itself, and `ExpenseCategory` is `TenantScopedModel`-backed (RLS-protected,
T0.5): an INSERT with no `app.current_tenant` GUC set would be silently
rejected by the `WITH CHECK` policy under the RLS-subject `cortex_app` role.
Idempotent (`all_objects.get_or_create`), safe to re-run.
"""

from __future__ import annotations

from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.tenancy.context import tenant_context
from apps.tenancy.models import Tenant

from .models import ExpenseCategory

# Keep in lockstep with `apps.projects.migrations.0006_seed_expense_categories.
# DEFAULT_CATEGORIES` (docs/tasks/M7-project-grants.md / data-model.md §2
# starter set) — that migration's own list is pinned on-disk deliberately
# (migration convention, same as `apps.rbac.migrations.0002`'s literal
# `PERMISSION_LABELS` snapshot), so this is a second, intentionally-duplicated
# copy for the "going forward" path rather than an import from a migration
# module (importing FROM a migration is the anti-pattern here, not the other
# way around).
DEFAULT_EXPENSE_CATEGORIES = [
    "Equipment",
    "Consumables",
    "Services",
    "Software",
    "Travel",
    "Shipping",
    "Other",
]


@receiver(post_save, sender=Tenant)
def seed_expense_categories(sender, instance: Tenant, created: bool, **kwargs) -> None:
    if not created:
        return
    with tenant_context(instance.id):
        for name in DEFAULT_EXPENSE_CATEGORIES:
            ExpenseCategory.all_objects.get_or_create(
                tenant=instance,
                name=name,
                defaults={"is_active": True},
            )
