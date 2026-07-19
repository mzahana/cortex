"""Idempotent RBAC seed logic (T0.4).

Shared by:
- the data migration (`migrations/0002_seed_permissions.py`), which seeds the
  global `Permission` rows once, and backfills roles for any tenants that
  already existed at migration time;
- `signals.py`, which seeds the 4 system roles for every **newly created**
  `Tenant` going forward, and assigns the default **Member** role to every
  **newly created** `User` (least privilege, docs/rbac.md §5).

Kept plain-Python (not tied to migration `apps.get_model` vs. real model
imports) so it can be called from both contexts; callers pass in the model
classes they have available (real models at runtime, historical models from
`apps.get_model(...)` inside a migration).
"""

from __future__ import annotations

from typing import Any

from .permission_keys import (
    DEFAULT_ROLE_KEY,
    PERMISSION_LABELS,
    ROLE_NAMES,
    SYSTEM_ROLE_PERMISSIONS,
)


def seed_permissions(permission_model: Any) -> dict[str, Any]:
    """Create/update the global `Permission` rows. Returns key -> instance."""
    by_key: dict[str, Any] = {}
    for key, label in PERMISSION_LABELS.items():
        obj, _ = permission_model.objects.get_or_create(key=key, defaults={"label": label})
        if obj.label != label:
            obj.label = label
            obj.save(update_fields=["label"])
        by_key[key] = obj
    return by_key


def seed_roles_for_tenant(
    *,
    tenant: Any,
    role_model: Any,
    permission_model: Any,
    role_permission_model: Any,
) -> dict[str, Any]:
    """Create/update the 4 system roles + grants for one tenant.

    Idempotent: safe to call repeatedly (e.g. re-run for every request in
    tests, or from a management command) — uses `get_or_create` throughout.
    """
    permissions_by_key = seed_permissions(permission_model)

    # Seeding is a deliberate, reviewable system operation (migrations/signals
    # run outside any authenticated request), so it uses the unscoped
    # `all_objects` manager rather than requiring a matching tenant context —
    # see `apps.tenancy.models.TenantScopedModel` docstring.
    roles_by_key: dict[str, Any] = {}
    for role_key, name in ROLE_NAMES.items():
        role, _ = role_model.all_objects.get_or_create(
            tenant=tenant,
            key=role_key,
            defaults={"name": name, "is_system": True},
        )
        if not role.is_system or role.name != name:
            role.is_system = True
            role.name = name
            role.save(update_fields=["is_system", "name"])
        roles_by_key[role_key] = role

        for perm_key in SYSTEM_ROLE_PERMISSIONS[role_key]:
            role_permission_model.all_objects.get_or_create(
                tenant=tenant,
                role=role,
                permission=permissions_by_key[perm_key],
            )
    return roles_by_key


def default_role_for_tenant(*, tenant: Any, role_model: Any) -> Any:
    """Return the tenant's Member role, seeding it (and its siblings) first
    if this tenant hasn't been seeded yet."""
    role, _created = role_model.all_objects.get_or_create(
        tenant=tenant,
        key=DEFAULT_ROLE_KEY,
        defaults={"name": ROLE_NAMES[DEFAULT_ROLE_KEY], "is_system": True},
    )
    return role
