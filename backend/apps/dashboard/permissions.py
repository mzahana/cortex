"""RBAC for `GET /api/v1/dashboard/summary` (T5.5).

Read-only, list-shaped (no single object -- every tile is already an
aggregate over the caller's OWN viewable scope), so the same two-phase
pattern other scoped read endpoints use collapses to just the permissive
list-time gate: "does this user hold `asset.view` ANYWHERE (tenant-wide or
in at least one project)?" (`apps.rbac.services.
user_has_permission_in_any_scope`, same helper `apps.audit.permissions.
AuditLogPermission`/`apps.assets.permissions.AssetPermission`'s `list` action
use). The actual row-level restriction per tile is applied inside
`apps.dashboard.services.get_dashboard_summary` via
`get_viewable_project_scope`, exactly like every other scoped list endpoint's
`get_queryset`.
"""

from __future__ import annotations

from rest_framework.permissions import BasePermission

from apps.rbac.permission_keys import ASSET_VIEW
from apps.rbac.services import user_has_permission_in_any_scope


class DashboardSummaryPermission(BasePermission):
    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False
        return user_has_permission_in_any_scope(user, ASSET_VIEW)
