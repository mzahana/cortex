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

**Migration callers MUST pass historical models** (and the `using` alias).
The concrete classes describe today's schema, which is *not* the schema a
migration runs against: `rbac.0003`'s forward seed used to pass the concrete
`Role`, whose `get_or_create` therefore SELECTed `rbac_role.is_customized` —
a column `rbac.0004` only adds one migration later. That made forward
`migrate` fail with `column rbac_role.is_customized does not exist` on any
database that already had tenant rows (CI only stayed green because its
database had zero tenants, so the seed loop body never ran). Hence
`unscoped()` below, which builds a plain `QuerySet` and so works for both
historical models (no `all_objects` — only `objects` is serialized into
migration state) and concrete ones.
"""

from __future__ import annotations

from typing import Any

from django.db import models

from .permission_keys import (
    DEFAULT_ROLE_KEY,
    PERMISSION_LABELS,
    ROLE_NAMES,
    SYSTEM_ROLE_PERMISSIONS,
)


def unscoped(model: Any, using: str | None = None) -> models.QuerySet:
    """An UNFILTERED queryset for `model`, live *or* historical.

    Seeding is a deliberate, reviewable system operation (migrations/signals
    run outside any authenticated request), so it must bypass the fail-closed
    `TenantScopedManager` — see `apps.tenancy.models.TenantScopedModel`. At
    runtime the documented escape hatch is the `all_objects` manager; historical
    models don't have one (only `objects` is serialized into migration state,
    via `use_in_migrations`), so build a plain `QuerySet` bound to the model,
    bypassing the managers entirely. Equivalent to `model._base_manager` for
    the non-scoped case, and works unchanged for models that aren't
    tenant-owned at all (e.g. `Permission`).
    """
    qs: models.QuerySet = models.QuerySet(model=model)
    return qs.using(using) if using is not None else qs


def seed_permissions(permission_model: Any, using: str | None = None) -> dict[str, Any]:
    """Create/update the global `Permission` rows. Returns key -> instance."""
    by_key: dict[str, Any] = {}
    for key, label in PERMISSION_LABELS.items():
        obj, _ = unscoped(permission_model, using).get_or_create(key=key, defaults={"label": label})
        if obj.label != label:
            obj.label = label
            obj.save(using=using, update_fields=["label"])
        by_key[key] = obj
    return by_key


def seed_roles_for_tenant(
    *,
    tenant: Any,
    role_model: Any,
    permission_model: Any,
    role_permission_model: Any,
    using: str | None = None,
) -> dict[str, Any]:
    """Create/update the 4 system roles + grants for one tenant.

    Idempotent: safe to call repeatedly (e.g. re-run for every request in
    tests, or from a management command) — uses `get_or_create` throughout.

    `using` is the database alias; migrations should pass
    `schema_editor.connection.alias` (along with historical models), runtime
    callers can leave it as `None` to use the router's default.
    """
    permissions_by_key = seed_permissions(permission_model, using)

    roles_by_key: dict[str, Any] = {}
    for role_key, name in ROLE_NAMES.items():
        role, _ = unscoped(role_model, using).get_or_create(
            tenant=tenant,
            key=role_key,
            defaults={"name": name, "is_system": True},
        )
        if not role.is_system or role.name != name:
            role.is_system = True
            role.name = name
            role.save(using=using, update_fields=["is_system", "name"])
        roles_by_key[role_key] = role

        # `getattr` default, not `role.is_customized`: a historical `Role` from
        # a migration whose state predates `rbac.0004` has no such field — and
        # correctly so, since role customization didn't exist yet at that point
        # in history, so every default grant should be (re-)seeded.
        if getattr(role, "is_customized", False):
            # An admin has edited this role's grants (docs/rbac.md §6). Re-
            # seeding is `get_or_create`-based, so without this guard a later
            # re-run (migration backfill, management command) would silently
            # re-add every default grant they removed — including, say, the
            # `category.manage` they took away from Project Lead. "Reset to
            # defaults" (`POST /api/v1/roles/{id}/reset`) is the ONLY way
            # back to the defaults once customized.
            continue

        for perm_key in SYSTEM_ROLE_PERMISSIONS[role_key]:
            unscoped(role_permission_model, using).get_or_create(
                tenant=tenant,
                role=role,
                permission=permissions_by_key[perm_key],
            )
    return roles_by_key


def default_role_for_tenant(*, tenant: Any, role_model: Any, using: str | None = None) -> Any:
    """Return the tenant's Member role, seeding it (and its siblings) first
    if this tenant hasn't been seeded yet."""
    role, _created = unscoped(role_model, using).get_or_create(
        tenant=tenant,
        key=DEFAULT_ROLE_KEY,
        defaults={"name": ROLE_NAMES[DEFAULT_ROLE_KEY], "is_system": True},
    )
    return role
