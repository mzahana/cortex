"""RBAC gate for `POST /api/v1/labels/generate` (T4.5, docs/rbac.md §3:
`label.generate` — Admin ✅ tenant-wide, ProjectLead 🟡 scoped to their own
project's assets, Member/Viewer ➖).

Same two-phase shape `apps.assets.permissions` uses and for the identical
reason: `has_permission()` runs before any object is known (this is a plain
`APIView`, not object-detail routing, but the REQUEST names potentially many
assets spanning multiple projects, so there is no single "the object" to
check against at the DRF-permission-class level either way) — it only proves
the caller holds `label.generate` SOMEWHERE (deliberately permissive, avoids
over-denying a pure ProjectLead with no tenant-wide grant). The real,
per-asset, scope-correct decision — which of the requested asset ids the
caller may actually generate a label for — happens in
`apps.labels.api.LabelGenerateView.post` via `user_has_permission(request.
user, LABEL_GENERATE, project=asset.project_id)` for each asset, same rule
`apps.assets.permissions.AssetPermission.has_object_permission` already
applies for `asset.edit`/`asset.retire`/etc.
"""

from __future__ import annotations

from rest_framework.permissions import BasePermission

from apps.rbac.permission_keys import LABEL_GENERATE
from apps.rbac.services import user_has_permission_in_any_scope


class LabelGeneratePermission(BasePermission):
    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False
        return user_has_permission_in_any_scope(user, LABEL_GENERATE)
