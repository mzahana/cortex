"""DRF permission classes for the catalog admin-config endpoints (T1.1).

`Category`, `Location`, and `Project` are **tenant-wide** admin configuration
— unlike `Asset`, they carry no `project_id` of their own to scope against
(you cannot meaningfully restrict "create a Project" to "within a project"),
so every check here resolves permissions with `project=None` deliberately,
never against `request.data`/an object's project. This matches
`docs/rbac.md` §1's own framing of tenant-wide vs. project-scoped grants:
these resources are governed exclusively by tenant-wide memberships.

**Read/write split, and the permission keys chosen (flagged assumptions —
`docs/rbac.md`'s matrix has no dedicated key for these three resources'
*read* access, nor for `Project`'s writes at all):**

- `Category`/`Location`: read (`GET`) requires `asset.view` (the same
  permission that gates seeing the asset list at all — the category/location
  trees are inventory metadata every viewer needs for filtering); writes
  require the documented `category.manage` / `location.manage` key
  (Admin-only per the matrix).
- `Project`: read requires `asset.view` (needed to filter/browse by
  project); writes require `tenant.manage` (Admin-only) — the matrix has no
  `project.manage` key, and Project is structural tenant configuration in the
  same spirit as `tenant.manage`'s other admin-only actions (ProjectLeads are
  *assigned* to a project via `Membership`/`user.manage`+`role.assign`, they
  do not create/delete the `Project` row itself). Flagged for
  code-reviewer/product to confirm.
- `Tag`: read-only endpoint per `docs/api-and-ui.md` (`GET /api/v1/tags`
  only); gated by `asset.view` alone, no manage key needed.
"""

from __future__ import annotations

from rest_framework.permissions import SAFE_METHODS, BasePermission

from apps.rbac.permission_keys import ASSET_VIEW
from apps.rbac.services import user_has_permission


class TenantWideReadOrManage(BasePermission):
    """`GET`/`HEAD`/`OPTIONS` -> `view_key` (default `asset.view`); every
    other method -> `manage_key`. Always evaluated tenant-wide
    (`project=None`) — see module docstring for why these resources have no
    project scope of their own.
    """

    def __init__(self, manage_key: str, view_key: str = ASSET_VIEW):
        self.manage_key = manage_key
        self.view_key = view_key

    def __call__(self):
        # Mirrors `apps.rbac.permissions.HasPermission.__call__`: DRF
        # instantiates `permission_classes` entries with no args, so a
        # pre-built instance must be callable to return itself.
        return self

    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False
        key = self.view_key if request.method in SAFE_METHODS else self.manage_key
        return user_has_permission(user, key, project=None)

    def has_object_permission(self, request, view, obj) -> bool:
        # Same tenant-wide rule at the object level — these models have no
        # `project`/`project_id` to re-check against.
        return self.has_permission(request, view)


class TenantWideView(BasePermission):
    """Read-only tenant-wide check: `Tag`'s endpoint (docs/api-and-ui.md:
    `GET /api/v1/tags` only) exposes no write action at all, so there is no
    "manage" key to require — just `view_key` (default `asset.view`),
    evaluated tenant-wide like `TenantWideReadOrManage` above.
    """

    def __init__(self, view_key: str = ASSET_VIEW):
        self.view_key = view_key

    def __call__(self):
        return self

    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False
        return user_has_permission(user, self.view_key, project=None)

    def has_object_permission(self, request, view, obj) -> bool:
        return self.has_permission(request, view)
