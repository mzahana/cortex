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
