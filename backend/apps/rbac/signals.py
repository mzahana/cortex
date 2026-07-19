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
from apps.tenancy.models import Tenant

from .models import Membership, Permission, Role, RolePermission
from .seed import default_role_for_tenant, seed_roles_for_tenant


@receiver(post_save, sender=Tenant)
def seed_system_roles(sender, instance: Tenant, created: bool, **kwargs) -> None:
    if not created:
        return
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
    member_role = default_role_for_tenant(tenant=instance.tenant, role_model=Role)
    Membership.all_objects.get_or_create(
        tenant=instance.tenant,
        user=instance,
        role=member_role,
        project=None,
    )
