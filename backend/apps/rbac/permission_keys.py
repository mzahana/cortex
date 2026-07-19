"""The permission-key set and system-role grants — verbatim from
`docs/rbac.md` §2/§3 (the authority for both). Kept as plain Python constants
(not DB rows) so the seed migration/signals and any code that needs to
reference a key literal (e.g. `@requires_permission(ASSET_CREATE)`) share one
source of truth and can't drift from the matrix.

Legend from rbac.md: ✅ allowed (tenant-wide) · 🟡 scoped (only within a
project the user leads) · ➖ denied. 🟡 is modeled as: the permission IS
granted to the ProjectLead role, but only ever evaluated against
project-scoped Memberships — see `apps.rbac.services.get_effective_permissions`
for how the tenant-wide-vs-project-scoped distinction is actually enforced.
"""

from __future__ import annotations

# --- Permission keys (docs/rbac.md §3, one row per matrix line) --------------
ASSET_VIEW = "asset.view"
ASSET_EXPORT = "asset.export"
ASSET_CREATE = "asset.create"
ASSET_EDIT = "asset.edit"
ASSET_RETIRE = "asset.retire"
ASSET_ATTACH = "asset.attach"
CATEGORY_MANAGE = "category.manage"
LOCATION_MANAGE = "location.manage"
STOCK_ADJUST = "stock.adjust"
STOCK_CONSUME = "stock.consume"
REORDER_REQUEST = "reorder.request"
REORDER_APPROVE = "reorder.approve"
RESERVATION_CREATE = "reservation.create"
RESERVATION_APPROVE = "reservation.approve"
CHECKOUT_MANAGE = "checkout.manage"
CHECKOUT_OVERRIDE = "checkout.override"
ISSUE_REPORT = "issue.report"
MAINTENANCE_MANAGE = "maintenance.manage"
LABEL_GENERATE = "label.generate"
IMPORT_RUN = "import.run"
USER_MANAGE = "user.manage"
ROLE_ASSIGN = "role.assign"
AUDIT_VIEW = "audit.view"
TENANT_MANAGE = "tenant.manage"
NOTIFY_SELF = "notify.self"

# `key -> human label`, used by the seed migration to create `Permission` rows.
PERMISSION_LABELS: dict[str, str] = {
    ASSET_VIEW: "View inventory / assets",
    ASSET_EXPORT: "Search / filter / export asset list",
    ASSET_CREATE: "Add asset",
    ASSET_EDIT: "Edit asset",
    ASSET_RETIRE: "Retire / mark lost",
    ASSET_ATTACH: "Upload photo/attachment",
    CATEGORY_MANAGE: "Manage categories & custom fields",
    LOCATION_MANAGE: "Manage locations",
    STOCK_ADJUST: "Adjust stock / receive",
    STOCK_CONSUME: "Consume stock",
    REORDER_REQUEST: "Request reorder",
    REORDER_APPROVE: "Approve reorder",
    RESERVATION_CREATE: "Create reservation",
    RESERVATION_APPROVE: "Approve/reject reservation",
    CHECKOUT_MANAGE: "Check out / check in",
    CHECKOUT_OVERRIDE: "Force-return / override checkout",
    ISSUE_REPORT: "Report an issue",
    MAINTENANCE_MANAGE: "Schedule/log maintenance",
    LABEL_GENERATE: "Generate labels / PDF",
    IMPORT_RUN: "Bulk import",
    USER_MANAGE: "Manage users & memberships",
    ROLE_ASSIGN: "Assign roles",
    AUDIT_VIEW: "View audit log",
    TENANT_MANAGE: "Manage tenant settings",
    NOTIFY_SELF: "Configure own notifications",
}

# --- System roles (docs/rbac.md §2) -----------------------------------------
ROLE_ADMIN = "admin"
ROLE_PROJECT_LEAD = "project_lead"
ROLE_MEMBER = "member"
ROLE_VIEWER = "viewer"

ROLE_NAMES: dict[str, str] = {
    ROLE_ADMIN: "Admin",
    ROLE_PROJECT_LEAD: "Project Lead",
    ROLE_MEMBER: "Member",
    ROLE_VIEWER: "Viewer",
}

# The role -> permission keys a user of that role holds when the Membership
# granting it is TENANT-WIDE (project=NULL). This is exactly the "Admin"/
# "Member"/"Viewer" columns of the matrix, which have no 🟡 scoped cells.
ADMIN_PERMISSIONS: frozenset[str] = frozenset(
    {
        ASSET_VIEW,
        ASSET_EXPORT,
        ASSET_CREATE,
        ASSET_EDIT,
        ASSET_RETIRE,
        ASSET_ATTACH,
        CATEGORY_MANAGE,
        LOCATION_MANAGE,
        STOCK_ADJUST,
        STOCK_CONSUME,
        REORDER_REQUEST,
        REORDER_APPROVE,
        RESERVATION_CREATE,
        RESERVATION_APPROVE,
        CHECKOUT_MANAGE,
        CHECKOUT_OVERRIDE,
        ISSUE_REPORT,
        MAINTENANCE_MANAGE,
        LABEL_GENERATE,
        IMPORT_RUN,
        USER_MANAGE,
        ROLE_ASSIGN,
        AUDIT_VIEW,
        TENANT_MANAGE,
        NOTIFY_SELF,
    }
)

# Project Lead's full matrix column (both ✅ and 🟡 cells) — every 🟡 cell in
# the matrix. Whether it actually reaches an asset is decided at query time by
# `get_effective_permissions`: a Membership granting this role is only
# effective for the project it's scoped to (never tenant-wide/general-pool)
# UNLESS the same user separately holds a tenant-wide membership.
PROJECT_LEAD_PERMISSIONS: frozenset[str] = frozenset(
    {
        ASSET_VIEW,
        ASSET_EXPORT,
        ASSET_CREATE,
        ASSET_EDIT,
        ASSET_RETIRE,
        ASSET_ATTACH,
        STOCK_ADJUST,
        STOCK_CONSUME,
        REORDER_REQUEST,
        REORDER_APPROVE,
        RESERVATION_CREATE,
        RESERVATION_APPROVE,
        CHECKOUT_MANAGE,
        CHECKOUT_OVERRIDE,
        ISSUE_REPORT,
        MAINTENANCE_MANAGE,
        LABEL_GENERATE,
        USER_MANAGE,  # footnote 3: within their own project only
        ROLE_ASSIGN,  # footnote 3: within their own project only
        AUDIT_VIEW,  # footnote 4: their project's assets only
        NOTIFY_SELF,
    }
)

MEMBER_PERMISSIONS: frozenset[str] = frozenset(
    {
        ASSET_VIEW,
        ASSET_EXPORT,
        ASSET_ATTACH,  # footnote 1: assets they hold/are editing via a report
        STOCK_CONSUME,
        REORDER_REQUEST,
        RESERVATION_CREATE,
        CHECKOUT_MANAGE,  # footnote 2: may require approval per category config
        ISSUE_REPORT,
        NOTIFY_SELF,
    }
)

VIEWER_PERMISSIONS: frozenset[str] = frozenset(
    {
        ASSET_VIEW,
        NOTIFY_SELF,
    }
)

SYSTEM_ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    ROLE_ADMIN: ADMIN_PERMISSIONS,
    ROLE_PROJECT_LEAD: PROJECT_LEAD_PERMISSIONS,
    ROLE_MEMBER: MEMBER_PERMISSIONS,
    ROLE_VIEWER: VIEWER_PERMISSIONS,
}

# Least privilege: new users default to Member (docs/rbac.md §5).
DEFAULT_ROLE_KEY = ROLE_MEMBER
