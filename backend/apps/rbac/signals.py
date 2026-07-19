"""Auto-seeding signals (T0.4).

- New `Tenant` -> seed its 4 system roles + permission grants.
- New `User` -> grant them a tenant-wide **Member** membership (least
  privilege default, docs/rbac.md §5 / M0 exit criterion).

Both handlers are idempotent (`seed.py` uses `get_or_create` throughout), so
re-running them (e.g. via `migrate` + a subsequent signal in tests) is safe.
"""

from __future__ import annotations

from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.accounts.models import User
from apps.tenancy.context import tenant_context
from apps.tenancy.models import Tenant

from .models import Membership, Permission, Role, RolePermission
from .seed import default_role_for_tenant, seed_roles_for_tenant


@receiver(post_save, sender=Tenant)
def seed_system_roles(sender, instance: Tenant, created: bool, **kwargs) -> None:
    if not created:
        return
    # PREREQUISITE FIX (M1 T1.1, carried M0 finding): this handler fires
    # inside `Tenant.objects.get_or_create(...)` — i.e. before the caller has
    # (or can have) entered `tenant_context(instance.id)`, since the tenant's
    # own id isn't known/committed-to-context until this very save returns.
    # `seed_roles_for_tenant` uses `all_objects.get_or_create` (deliberately
    # unscoped at the *application* filter layer, see `seed.py`), but `Role`/
    # `RolePermission` are still `TenantScopedModel`s protected by Postgres
    # RLS (T0.5). Under the RLS-subject `cortex_app` role (not the migration
    # owner), an INSERT with no `app.current_tenant` GUC set evaluates the
    # `WITH CHECK` predicate as `tenant_id = NULL`, which is always false for
    # a real tenant row -> every insert here was silently rejected, breaking
    # `Tenant.objects.get_or_create(...)` (and `seed_t0_6`) on a fresh DB.
    # Fix: explicitly enter `tenant_context(instance.id)` for the duration of
    # the seeding so the GUC (and the app-level contextvar, used nowhere here
    # since `all_objects` ignores it, but kept in lockstep regardless) matches
    # the tenant whose rows we're inserting.
    with tenant_context(instance.id):
        seed_roles_for_tenant(
            tenant=instance,
            role_model=Role,
            permission_model=Permission,
            role_permission_model=RolePermission,
        )


@receiver(post_save, sender=User)
def assign_default_membership(sender, instance: User, created: bool, **kwargs) -> None:
    if not created:
        return
    # Same RLS-GUC reasoning as `seed_system_roles` above: don't assume the
    # caller has already entered `tenant_context(instance.tenant_id)` (e.g. a
    # `User` created directly against a fresh tenant before any request/seed
    # loop has scoped this connection) — enter it explicitly so the
    # `Membership` insert satisfies RLS's `WITH CHECK` under `cortex_app`.
    with tenant_context(instance.tenant_id):
        member_role = default_role_for_tenant(tenant=instance.tenant, role_model=Role)
        Membership.all_objects.get_or_create(
            tenant=instance.tenant,
            user=instance,
            role=member_role,
            project=None,
        )
