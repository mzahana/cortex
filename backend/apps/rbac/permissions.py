"""DRF permission-class scaffolding for future endpoints (T0.4).

Views apply this **after** tenant isolation (the queryset already only
contains the caller's tenant's rows via the base manager / RLS) — this class
only ever answers "does this user's RBAC allow `permission_key` here", never
"which tenant". See `docs/rbac.md` §5: "Server-side only... Tenant isolation
is orthogonal and always applied first."
"""

from __future__ import annotations

from rest_framework.permissions import BasePermission

from apps.projects.models import Project

from .models import Membership
from .permission_keys import ROLE_ADMIN, ROLE_ASSIGN, ROLE_MEMBER, ROLE_PROJECT_LEAD, USER_MANAGE
from .services import (
    get_viewable_project_scope,
    user_has_permission,
    user_has_permission_in_any_scope,
)


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


def _resolve_target_project_id(request) -> int | None:
    """The target `project` for a `create`/`update` Membership request,
    re-scoped through the tenant-scoped `Project.objects` queryset (R4:
    never trust a client-supplied id blindly) -- same pattern as
    `apps.assets.permissions._resolve_target_project_id`."""
    raw = request.data.get("project")
    if raw in (None, "", "null"):
        return None
    try:
        project_id = int(raw)
    except (TypeError, ValueError):
        return None
    if Project.objects.filter(pk=project_id).exists():
        return project_id
    return None


def _resolve_role_key(request) -> str | None:
    """The `Role.key` a `create`/`update` Membership request is trying to
    grant, re-scoped through the tenant-scoped `Role.objects` queryset --
    needed to enforce docs/rbac.md §3 footnote 3 ("Project Lead may ...
    assign the Member role ONLY within their own project; cannot create
    Admins")."""
    from .models import Role  # local import: avoids a module-load cycle with `.models`

    raw = request.data.get("role")
    if raw in (None, "", "null"):
        return None
    try:
        role_id = int(raw)
    except (TypeError, ValueError):
        return None
    role = Role.objects.filter(pk=role_id).only("key").first()
    return role.key if role else None


MEMBERSHIP_ACTION_PERMISSION_MAP: dict[str, str] = {
    "list": USER_MANAGE,
    "retrieve": USER_MANAGE,
    "create": USER_MANAGE,
    "update": ROLE_ASSIGN,
    "partial_update": ROLE_ASSIGN,
    "destroy": USER_MANAGE,
}


class MembershipPermission(BasePermission):
    """`MembershipViewSet` (T5.3 gap-fill, `docs/rbac.md` §3 matrix rows
    `user.manage`/`role.assign`, footnote 3):

    - Admin (a TENANT-WIDE grant of `user.manage`/`role.assign`): full
      control -- any user, any role, any project or tenant-wide scope.
    - Project Lead (a PROJECT-SCOPED grant only, footnote 3): may add/remove
      members and assign the **Member** role, but ONLY within their own
      project -- never tenant-wide, never a Member/ProjectLead/Admin/Viewer
      role other than Member, never another project.

    Same two-phase pattern as `apps.assets.permissions.AssetPermission` /
    `apps.reservations.permissions.ReservationPermission`: `has_permission`
    is a permissive "holds it somewhere, and — for create — the request's
    OWN declared scope is plausible" gate; `has_object_permission` makes the
    real, scope-correct decision once the actual `Membership.project_id` (and,
    for update, the object's CURRENT role) is known.
    """

    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False

        action = getattr(view, "action", "") or ""
        permission_key = MEMBERSHIP_ACTION_PERMISSION_MAP.get(action)
        if permission_key is None:
            return False  # fail-closed: unmapped action

        if action == "create":
            project_id = _resolve_target_project_id(request)
            if not user_has_permission(user, USER_MANAGE, project=project_id):
                return False
            tenant_wide, _ = get_viewable_project_scope(user, USER_MANAGE)
            if not tenant_wide:
                # Project Lead footnote 3: own project only, Member role only.
                if project_id is None:
                    return False
                if _resolve_role_key(request) != ROLE_MEMBER:
                    return False
            return True

        if action == "list":
            return user_has_permission_in_any_scope(
                user, USER_MANAGE
            ) or user_has_permission_in_any_scope(user, ROLE_ASSIGN)

        # retrieve/update/partial_update/destroy: provisional "holds it
        # somewhere" gate; has_object_permission makes the real call.
        return user_has_permission_in_any_scope(user, permission_key)

    def has_object_permission(self, request, view, obj: Membership) -> bool:
        user = request.user
        action = getattr(view, "action", "") or ""
        permission_key = MEMBERSHIP_ACTION_PERMISSION_MAP.get(action)
        if permission_key is None:
            return False

        project_id = obj.project_id
        if not user_has_permission(user, permission_key, project=project_id):
            return False
        if not user_has_permission(user, USER_MANAGE, project=project_id):
            return False

        tenant_wide, _ = get_viewable_project_scope(user, USER_MANAGE)
        if tenant_wide:
            return True  # Admin: full control (docs/rbac.md §3, ✅ row)

        # Project Lead (project-scoped grant only, footnote 3): never a
        # tenant-wide membership (Admin-only territory), never an Admin OR
        # a fellow Project Lead's membership (footnote 3 grants add/remove
        # of MEMBERS only -- a lead demoting/removing a co-lead is outside
        # that grant, even within their own project), and any role CHANGE
        # they make must land on Member.
        if project_id is None:
            return False
        if obj.role.key in (ROLE_ADMIN, ROLE_PROJECT_LEAD):
            return False
        if action in ("update", "partial_update"):
            new_role_key = _resolve_role_key(request)
            if new_role_key is not None and new_role_key != ROLE_MEMBER:
                return False
        return True
