"""RBAC for `GET /api/v1/audit` (T5.3, `docs/rbac.md` §3 `audit.view` row +
footnote 4: "Project Lead sees audit entries for their project's assets
only").

Two-phase pattern, same as every other scoped endpoint in this codebase
(`apps.assets.permissions`, `apps.reservations.permissions`, ...): this class
only answers the "list" gate (permissive: "holds `audit.view` somewhere?");
the REAL, scope-correct row-level restriction (Admin tenant-wide vs.
ProjectLead their-project-only) is applied in `AuditLogViewSet.get_queryset`
via `apps.rbac.services.get_viewable_project_scope`, exactly like
`apps.assets.api.AssetViewSet.get_queryset` restricts `list` for `asset.view`.
This is a read-only endpoint (no create/update/destroy actions exist at all),
so there is no object-level write path to gate here.
"""

from __future__ import annotations

from rest_framework.permissions import BasePermission

from apps.rbac.permission_keys import AUDIT_VIEW
from apps.rbac.services import user_has_permission_in_any_scope


class AuditLogPermission(BasePermission):
    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False
        return user_has_permission_in_any_scope(user, AUDIT_VIEW)
