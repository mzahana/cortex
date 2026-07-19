"""Union-of-memberships effective-permission resolver (T0.4, docs/rbac.md §1).

This is the one place that implements the scope-resolution rule so every
caller (the `/me` endpoint in T0.6, future DRF permission classes, tests)
gets identical behavior:

    1. Tenant isolation first — always applied by whoever fetched the asset/
       object in the first place (base manager / RLS). This module does not
       re-derive or check the tenant; it only resolves RBAC *within* the
       tenant a `Membership` already belongs to.
    2. A tenant-wide membership (`project=None`) grants its role's
       permissions unconditionally.
    3. A project-scoped membership (`project=P`) grants its role's
       permissions only for actions scoped to that same project `P`.
    4. Anything not granted by (2) or (3) is denied.

General-pool assets (`project=None`) are therefore governed by tenant-wide
memberships only — a project-scoped Membership never "leaks" onto the shared
pool, matching docs/rbac.md §1 exactly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Union

from .models import Membership

if TYPE_CHECKING:
    from apps.projects.models import Project

ProjectLike = Union["Project", int, None]


def _project_id_of(project: ProjectLike) -> Optional[int]:
    if project is None:
        return None
    if isinstance(project, int):
        return project
    return project.id


def get_effective_permissions(user, project: ProjectLike = None) -> set[str]:
    """The set of permission keys `user` effectively holds for an action
    scoped to `project` (`None` = the tenant's general pool).

    Union across ALL of the user's memberships whose scope covers `project`:
    every tenant-wide membership, plus any project-scoped membership that
    matches `project` exactly.
    """
    project_id = _project_id_of(project)

    memberships = (
        Membership.objects.filter(user=user)
        .select_related("role")
        .prefetch_related("role__role_permissions__permission")
    )

    perms: set[str] = set()
    for membership in memberships:
        if membership.project_id is None:
            in_scope = True
        else:
            in_scope = project_id is not None and membership.project_id == project_id
        if not in_scope:
            continue
        perms.update(rp.permission.key for rp in membership.role.role_permissions.all())
    return perms


def user_has_permission(user, permission_key: str, project: ProjectLike = None) -> bool:
    """Convenience boolean wrapper around `get_effective_permissions`."""
    return permission_key in get_effective_permissions(user, project=project)


def get_viewable_project_scope(user, permission_key: str) -> tuple[bool, set[int]]:
    """Resolve the LIST-time (no single object yet) scope for `permission_key`:
    every project this user's memberships grant it in, plus whether ANY
    tenant-wide membership grants it (which — per docs/rbac.md §1 — is the
    ONLY way to reach the general pool AND implicitly covers every project
    too, tenant-wide).

    Returns `(tenant_wide, project_ids)`:
    - `tenant_wide=True` -> the caller may see every row in the tenant
      (general pool + every project), same as `user_has_permission(user,
      permission_key, project=None)` would allow for a single object.
    - `tenant_wide=False` -> the caller may see ONLY rows whose `project_id`
      is in `project_ids` (never the general pool — general-pool rows are
      governed by tenant-wide grants only, docs/rbac.md §1). `project_ids`
      may be empty (denied everywhere).

    This is what makes list-endpoint filtering match the same
    union-of-memberships rule `get_effective_permissions`/
    `user_has_permission` already enforce object-by-object — used by
    `apps.assets.api.AssetViewSet.get_queryset` (T1.4) to restrict the LIST
    queryset for a user whose ONLY grant of `asset.view` is project-scoped
    (the "pure ProjectLead" case docs/rbac.md/`apps.assets.permissions`
    already reason about for object-detail actions) so they see their own
    project's assets — never another project's, never the general pool —
    without being denied the list entirely (the M0 bug CLAUDE.md forbids
    reintroducing).
    """
    memberships = (
        Membership.objects.filter(user=user)
        .select_related("role")
        .prefetch_related("role__role_permissions__permission")
    )

    tenant_wide = False
    project_ids: set[int] = set()
    for membership in memberships:
        keys = {rp.permission.key for rp in membership.role.role_permissions.all()}
        if permission_key not in keys:
            continue
        if membership.project_id is None:
            tenant_wide = True
        else:
            project_ids.add(membership.project_id)
    return tenant_wide, project_ids


def user_has_permission_in_any_scope(user, permission_key: str) -> bool:
    """True if ANY of `user`'s memberships — tenant-wide OR project-scoped,
    in that membership's OWN scope — grant `permission_key`.

    This exists ONLY as the provisional `has_permission()` (list-level, no
    object yet) gate for **object-detail routes** (retrieve/update/retire/
    attach/...): DRF calls a permission class's `has_permission()` before
    `get_object()` even runs, so the object's actual `project` isn't known
    yet. Gating that step with plain tenant-wide `user_has_permission(...,
    project=None)` would over-deny a ProjectLead whose ONLY membership is
    project-scoped (never tenant-wide) — exactly the M0 "pure-ProjectLead
    collection-level over-deny" bug CLAUDE.md calls out to never
    reintroduce. This function is deliberately permissive (only proves the
    user holds the permission *somewhere*); the real, precise,
    scope-correct decision is always `user_has_permission(user,
    permission_key, project=<the object's actual project>)` in
    `has_object_permission()` afterwards — see `apps.assets.permissions`.
    """
    memberships = (
        Membership.objects.filter(user=user)
        .select_related("role")
        .prefetch_related("role__role_permissions__permission")
    )
    for membership in memberships:
        keys = {rp.permission.key for rp in membership.role.role_permissions.all()}
        if permission_key in keys:
            return True
    return False
