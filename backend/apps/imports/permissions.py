"""RBAC gates for T6.1's two new surfaces.

`import.run` (`docs/rbac.md` §3: "Bulk import — Admin ✅, Project Lead ➖,
Member ➖, Viewer ➖") is the one row in the matrix with **no 🟡 scoped
cell at all** — unlike `asset.create`/`asset.edit`/etc., a ProjectLead's
project-scoped grant never covers it (`apps.rbac.permission_keys.
PROJECT_LEAD_PERMISSIONS` deliberately omits `IMPORT_RUN`, so
`user_has_permission_in_any_scope` would already return `False` for a pure
ProjectLead) — this permission class checks the TENANT-WIDE grant
explicitly (`project=None`) rather than "holds it somewhere", both to make
that "no scoped variant" invariant explicit at the call site and to match
`asset.export`'s pattern of "state the exact rbac.md cell being enforced,
don't rely on it falling out of the permission-key sets by construction
alone".

`asset.export` (`docs/rbac.md` §3: "Admin ✅ tenant-wide, Project Lead 🟡
scoped, Member ✅ tenant-wide, Viewer ➖") DOES have a scoped cell, so
`AssetExportPermission` uses the same two-phase "holds it somewhere" gate
`apps.assets.permissions.AssetPermission` uses for `list` — the real,
row-level scope restriction happens in `apps.assets.api.
build_asset_list_queryset` via `get_viewable_project_scope`, reused
unchanged by `apps.imports.exports.AssetExportView`.
"""

from __future__ import annotations

from rest_framework.permissions import BasePermission

from apps.rbac.permission_keys import ASSET_EXPORT, IMPORT_RUN
from apps.rbac.services import user_has_permission, user_has_permission_in_any_scope


class ImportRunPermission(BasePermission):
    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False
        # Tenant-wide only (see module docstring) — `project=None`.
        return user_has_permission(user, IMPORT_RUN, project=None)


class AssetExportPermission(BasePermission):
    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False
        return user_has_permission_in_any_scope(user, ASSET_EXPORT)
