"""Scoped RBAC enforcement for the Asset endpoints (T1.2).

**The rule, exactly (docs/rbac.md §1, CLAUDE.md invariant):** assets are
project-scoped. For any action on asset A:
1. Tenant isolation already happened (the queryset that produced/will
   produce A is tenant-scoped — see `apps.assets.api`).
2. If the user holds the permission via a TENANT-WIDE membership -> allowed,
   regardless of `A.project_id` (including the general pool).
3. Else if `A.project_id = P` (not null) and the user holds the permission
   via a membership SCOPED TO `P` -> allowed.
4. Else denied (403).

General-pool assets (`project_id = NULL`) are therefore reachable only via
rule (2) — a ProjectLead's project-scoped grant never reaches them.

**Why this is two-phase, not one `HasPermission` check (the M0 bug this
must not reintroduce):** DRF calls `has_permission()` BEFORE the object
exists (list/create) and `has_object_permission()` AFTER `get_object()`
(retrieve/update/retire/attach). A ProjectLead's ONLY membership is often
project-scoped (no tenant-wide grant at all) — gating `has_permission()`
with a strict tenant-wide-only check would deny them on step 1 before
`has_object_permission()` ever got a chance to see the object's actual
project and correctly allow it. So:
- `create` has no object yet: the target project comes from the REQUEST
  BODY (`request.data["project"]`), re-scoped through the tenant-scoped
  `Project.objects` queryset (never trust the client's project id/tenant
  membership blindly) before being checked.
- `list` has no per-object relevance (`asset.view` is granted to every
  default role tenant-wide, docs/rbac.md §3) — checked tenant-wide only.
- Every other action (retrieve/update/partial_update/retire/attachments)
  uses `user_has_permission_in_any_scope` (deliberately permissive: "do they
  hold this key ANYWHERE?") at the `has_permission()` gate, then the real,
  precise, scope-correct check against the ACTUAL object in
  `has_object_permission()`.

**Re-parenting (code-review finding, fixed here):** an edit that changes
`project` moves the asset between scopes. Checking only the asset's
EXISTING `project_id` would let a ProjectLead scoped to project P PATCH an
asset in P and re-park it in project Q (or the general pool) without ever
holding `asset.edit` there — same tenant, so not an R4 leak, but it
violates docs/rbac.md §3's "🟡 only within the user's project" reading of
`asset.edit`. `has_object_permission()` therefore ALSO requires the
permission in the TARGET project (re-scoped the same way `create` resolves
its target project) whenever the request actually changes `project` — the
caller needs the permission in BOTH the source and destination scope;
moving to the general pool requires the tenant-wide grant, same as
`create`. A no-op (`project` sent but unchanged, or omitted entirely) never
triggers this extra check.
"""

from __future__ import annotations

from rest_framework.permissions import BasePermission

from apps.projects.models import Project
from apps.rbac.permission_keys import (
    ASSET_ATTACH,
    ASSET_CREATE,
    ASSET_EDIT,
    ASSET_RETIRE,
    ASSET_VIEW,
)
from apps.rbac.services import user_has_permission, user_has_permission_in_any_scope

# Maps a DRF viewset `.action` name to the permission key it requires
# (docs/rbac.md §3 matrix). Anything not listed here is denied by default
# (fail-closed) rather than silently allowed.
ACTION_PERMISSION_MAP: dict[str, str] = {
    "list": ASSET_VIEW,
    "retrieve": ASSET_VIEW,
    "create": ASSET_CREATE,
    "update": ASSET_EDIT,
    "partial_update": ASSET_EDIT,
    "retire": ASSET_RETIRE,
    "attachments": ASSET_ATTACH,
}


def _resolve_target_project_id(request) -> int | None:
    """The target `project` for a `create` request, re-scoped through the
    tenant-scoped `Project.objects` queryset. Returns `None` for "general
    pool" (absent/blank/`null`) AND for any id that does not resolve inside
    the caller's own tenant (R4: never trust a client-supplied FK blindly —
    a bogus/cross-tenant id is simply never treated as "this project", so it
    can never be used to smuggle a permission it doesn't actually cover; the
    write itself is separately rejected by the serializer's tenant-scoped
    `project` field either way).
    """
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


class AssetPermission(BasePermission):
    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False

        action = getattr(view, "action", "") or ""
        permission_key = ACTION_PERMISSION_MAP.get(action)
        if permission_key is None:
            return False  # fail-closed: unmapped action

        if action == "create":
            project_id = _resolve_target_project_id(request)
            return user_has_permission(user, permission_key, project=project_id)

        if action in ("list",):
            # T1.4: permissive "holds `asset.view` SOMEWHERE" gate, same
            # reasoning as the object-detail actions below — a pure
            # ProjectLead (project-scoped `asset.view` only, no tenant-wide
            # grant) must not be denied the list entirely (the M0 over-deny
            # bug). The precise row-level restriction (their project(s) only,
            # never another project or the general pool) is enforced by
            # `AssetViewSet.get_queryset` via
            # `apps.rbac.services.get_viewable_project_scope`, not here.
            return user_has_permission_in_any_scope(user, permission_key)

        # Object-detail actions: provisional "holds it somewhere" pass here;
        # `has_object_permission` below makes the real, scope-correct call.
        return user_has_permission_in_any_scope(user, permission_key)

    def has_object_permission(self, request, view, obj) -> bool:
        user = request.user
        action = getattr(view, "action", "") or ""
        permission_key = ACTION_PERMISSION_MAP.get(action)
        if permission_key is None:
            return False
        if not user_has_permission(user, permission_key, project=obj.project_id):
            return False

        # Re-parenting guard (see module docstring): only relevant to
        # update/partial_update, and only when the request actually supplies
        # a `project` key (a PATCH that never mentions `project` is a no-op
        # for this check).
        if action in ("update", "partial_update") and "project" in request.data:
            target_project_id = _resolve_target_project_id(request)
            if target_project_id != obj.project_id:
                if not user_has_permission(user, permission_key, project=target_project_id):
                    return False

        return True
