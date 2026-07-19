"""DRF permission-class scaffolding for future endpoints (T0.4).

Views apply this **after** tenant isolation (the queryset already only
contains the caller's tenant's rows via the base manager / RLS) — this class
only ever answers "does this user's RBAC allow `permission_key` here", never
"which tenant". See `docs/rbac.md` §5: "Server-side only... Tenant isolation
is orthogonal and always applied first."
"""

from __future__ import annotations

from rest_framework.permissions import BasePermission

from .services import user_has_permission


class HasPermission(BasePermission):
    """Usage: `permission_classes = [HasPermission("asset.create")]`.

    - `has_permission`: tenant-wide check (list/create views without a
      specific object yet).
    - `has_object_permission`: re-checks against the object's `project`
      (falls back to `None` / general-pool semantics if the object has no
      `project`/`project_id` attribute).
    """

    def __init__(self, permission_key: str):
        self.permission_key = permission_key

    def __call__(self):
        # DRF instantiates `permission_classes` entries with no args; this
        # lets `HasPermission("asset.create")` be listed directly (it's
        # already an instance) while still supporting the classic pattern of
        # subclassing per key if that's ever preferred.
        return self

    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False
        return user_has_permission(user, self.permission_key)

    def has_object_permission(self, request, view, obj) -> bool:
        user = request.user
        project = getattr(obj, "project", None)
        if project is None:
            project = getattr(obj, "project_id", None)
        return user_has_permission(user, self.permission_key, project=project)
