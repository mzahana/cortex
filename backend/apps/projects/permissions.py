"""RBAC enforcement for the M7 project hub (`docs/tasks/M7-project-grants.md`,
`docs/rbac.md` §3 additions).

**THE headline security property this module exists to guarantee (task's own
framing): a Project Lead of project A must get a server-side 403 — not an
empty list, not a 404 — when reading/writing project B's budget, expenses,
or documents.** `apps.rbac.services.get_effective_permissions`/
`user_has_permission` already implement the underlying union-of-memberships
rule (a project-scoped `Membership` is only ever effective for THAT SAME
project id); every `has_object_permission` below calls
`user_has_permission(user, <key>, project=<the actual resolved project>)`
— e.g. `project=obj.id` for `ProjectViewSet` (the object under test IS the
project) and `project=obj.project_id` for `Expense`/`ProjectDocument`/
`ExpenseAttachment` (project-scoped children, `docs/data-model.md` §2). This
is the ONE place in this module where the per-project 🟡 check actually
happens — every other method here is only the permissive "holds it
SOMEWHERE" collection-level gate (see the same two-phase pattern in
`apps.assets.permissions.AssetPermission`/`apps.rbac.permissions.
MembershipPermission`, needed because DRF calls `has_permission()` before
`get_object()` even runs).

Two-phase pattern, exactly like `apps.assets.permissions.AssetPermission`:
- `has_permission()` — no object yet (list/create, or before `get_object()`
  runs for retrieve/update/destroy/custom actions). A pure Project Lead's
  ONLY membership is often project-scoped (never tenant-wide), so this stays
  a permissive "holds the key ANYWHERE" gate — denying here outright would
  be the M0 over-deny bug CLAUDE.md forbids reintroducing.
- `has_object_permission()` — the real, scope-correct decision once the
  object (and therefore its actual project) is known.
"""

from __future__ import annotations

from rest_framework.permissions import SAFE_METHODS, BasePermission

from apps.rbac.permission_keys import (
    EXPENSE_MANAGE,
    EXPENSE_VIEW,
    PROJECT_MANAGE,
    PROJECT_VIEW,
    TENANT_MANAGE,
)
from apps.rbac.services import (
    get_viewable_project_scope,
    user_has_permission,
    user_has_permission_in_any_scope,
)

# --- ProjectViewSet (`/api/v1/projects`, `/{id}/assets`, `/{id}/expenses`,
# `/{id}/documents`) ----------------------------------------------------------

# Actions with no method-dependent split (see `_action_permission_key` below
# for `expenses`/`documents`, which differ GET vs POST).
PROJECT_ACTION_PERMISSION_MAP: dict[str, str] = {
    "list": PROJECT_VIEW,
    "retrieve": PROJECT_VIEW,
    "create": TENANT_MANAGE,  # structural CRUD stays Admin-only, unchanged from apps.catalog
    "destroy": TENANT_MANAGE,
    "update": PROJECT_MANAGE,
    "partial_update": PROJECT_MANAGE,
    "assets": PROJECT_VIEW,
    "export_csv": EXPENSE_VIEW,
    # Slice 3 (pwa-scan-specialist, `docs/tasks/M7-project-grants.md`): the
    # PDF report inlines the SAME budget/spend-by-category/itemized-ledger
    # figures `expense.view` already gates on `GET /projects/{id}` (code-
    # review finding #1/#2's redaction boundary, see `apps.projects.api`
    # module docstring) -- gating `report` on the tenant-wide-grantable
    # `project.view` would let a plain Member who can't see a single expense
    # row generate a PDF containing every one of them. `expense.view` scoped
    # to THIS project (Lead-of-that-project, or Admin) matches the same
    # financial boundary exactly.
    "report": EXPENSE_VIEW,
    # The ZIP bundle ships the ORIGINAL invoice scans and project documents
    # (proposal/contract/progress reports) — i.e. strictly more financial
    # material than the report PDF above, never less. Same gate, for the same
    # reason: `expense.view` scoped to THIS project.
    "archive": EXPENSE_VIEW,
}


def _action_permission_key(request, action: str) -> str | None:
    """Resolve the permission key for `action`, splitting the two
    read-or-write nested sub-resources (`expenses`, `documents`) by HTTP
    method.

    **Product decision (code-review finding #2): project documents
    (proposal/contract/progress_report) routinely contain the exact budget
    detail redacted elsewhere, so document READS get the SAME financial
    boundary as expenses — `expense.view` scoped to this project
    (Lead-of-that-project, or Admin) — not the tenant-wide-grantable
    `project.view` the original M7 spec text used for reads. Writes
    (`POST`) stay `project.manage`-gated, unchanged.
    """
    if action == "expenses":
        return EXPENSE_VIEW if request.method in SAFE_METHODS else EXPENSE_MANAGE
    if action == "documents":
        return EXPENSE_VIEW if request.method in SAFE_METHODS else PROJECT_MANAGE
    return PROJECT_ACTION_PERMISSION_MAP.get(action)


class ProjectPermission(BasePermission):
    """`apps.projects.api.ProjectViewSet` — detail/PATCH with budget rollup,
    plus the `assets`/`expenses`/`documents` sub-resource actions.
    """

    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False

        action = getattr(view, "action", "") or ""
        permission_key = _action_permission_key(request, action)
        if permission_key is None:
            return False  # fail-closed: unmapped action

        if action == "create":
            # Structural create has no project of its own to scope against
            # (a Project IS the scope) — tenant-wide only, same as
            # `apps.catalog.permissions.TenantWideReadOrManage` used to
            # enforce for this exact route.
            return user_has_permission(user, TENANT_MANAGE, project=None)

        if action == "list":
            # Permissive "holds `project.view` somewhere" gate; the real
            # row-level restriction (own project(s) only, never another
            # tenant's or a project this user isn't scoped to) is enforced
            # by `ProjectViewSet.get_queryset` via `get_viewable_project_scope`
            # — same pattern as `apps.assets.api.visible_assets_queryset`.
            return user_has_permission_in_any_scope(user, permission_key)

        # retrieve/update/partial_update/destroy/assets/expenses/documents:
        # permissive "holds it somewhere" gate here; `has_object_permission`
        # below makes the real, per-project call.
        return user_has_permission_in_any_scope(user, permission_key)

    def has_object_permission(self, request, view, obj) -> bool:
        user = request.user
        action = getattr(view, "action", "") or ""
        permission_key = _action_permission_key(request, action)
        if permission_key is None:
            return False

        if action == "destroy":
            # Structural delete stays Admin-only/tenant-wide (same reasoning
            # as `create` above) — never scoped to a Lead, even their own.
            return user_has_permission(user, TENANT_MANAGE, project=None)

        # THE per-project 🟡 scope check (see module docstring): `obj` IS the
        # `Project` here (this permission's object-level checks are only
        # ever invoked with a `Project` instance — `assets`/`expenses`/
        # `documents` all call `self.get_object()` first, exactly like
        # `apps.assets.api.AssetViewSet.retire`/`attachments` do), so
        # `project=obj.id` (equivalently `project=obj`) is the SPECIFIC
        # project in the URL — a Lead of a DIFFERENT project never matches
        # this membership filter, regardless of what else they lead.
        return user_has_permission(user, permission_key, project=obj.id)


def project_list_queryset_scope(user):
    """`(tenant_wide, project_ids)` for `PROJECT_VIEW`, used by
    `ProjectViewSet.get_queryset` to restrict `list` to exactly the projects
    this user's memberships actually cover — same helper/reasoning as
    `apps.assets.api.visible_assets_queryset`.
    """
    return get_viewable_project_scope(user, PROJECT_VIEW)


# --- ExpenseViewSet (`/api/v1/expenses/{id}`, `/{id}/attachment`) ------------

EXPENSE_ACTION_PERMISSION_MAP: dict[str, str | None] = {
    "retrieve": EXPENSE_VIEW,
    "update": EXPENSE_MANAGE,
    "partial_update": EXPENSE_MANAGE,
    "destroy": EXPENSE_MANAGE,
    "attachment": None,  # method-dependent, see `_expense_action_permission_key`
    # Copying an asset's PO/invoice onto this expense is an ordinary
    # attachment WRITE on the expense — same gate as uploading one. The
    # SOURCE asset is separately gated on `asset.view` inside the view
    # itself (`apps.projects.api.ExpenseViewSet.attachment_from_asset`),
    # since it lives outside this expense's project.
    "attachment_from_asset": EXPENSE_MANAGE,
}


def _expense_action_permission_key(request, action: str) -> str | None:
    if action == "attachment":
        return EXPENSE_VIEW if request.method in SAFE_METHODS else EXPENSE_MANAGE
    return EXPENSE_ACTION_PERMISSION_MAP.get(action)


class ExpensePermission(BasePermission):
    """Top-level `/api/v1/expenses/{id}` resource. `Expense` is
    project-scoped (`docs/data-model.md` §2) — the per-project 🟡 check here
    is `project=obj.project_id` (the expense's OWN project FK), matching
    `apps.assets.permissions.AssetPermission`'s `project=obj.project_id`
    exactly.
    """

    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False
        action = getattr(view, "action", "") or ""
        permission_key = _expense_action_permission_key(request, action)
        if permission_key is None:
            return False
        return user_has_permission_in_any_scope(user, permission_key)

    def has_object_permission(self, request, view, obj) -> bool:
        user = request.user
        action = getattr(view, "action", "") or ""
        permission_key = _expense_action_permission_key(request, action)
        if permission_key is None:
            return False
        # THE per-project 🟡 scope check for expenses: `obj.project_id` is
        # THIS expense's own project — a Lead scoped to a different project
        # never matches, even if `obj`'s id/pk were guessed (R4-adjacent:
        # tenant isolation already narrowed `get_queryset()` to this tenant;
        # this is the RBAC layer on top).
        return user_has_permission(user, permission_key, project=obj.project_id)


# --- ProjectDocumentViewSet (`/api/v1/documents/{id}`, destroy only) --------


class ProjectDocumentPermission(BasePermission):
    """Top-level `/api/v1/documents/{id}` — `DELETE` only (create/list are
    nested under `/projects/{id}/documents`, gated by `ProjectPermission`
    above). Gated by `project.manage`, scoped to `obj.project_id` — same
    per-project 🟡 rule as `ExpensePermission`.
    """

    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False
        return user_has_permission_in_any_scope(user, PROJECT_MANAGE)

    def has_object_permission(self, request, view, obj) -> bool:
        return user_has_permission(request.user, PROJECT_MANAGE, project=obj.project_id)


# --- ExpenseAttachmentViewSet (`/api/v1/expense-attachments/{id}`, destroy only)


class ExpenseAttachmentPermission(BasePermission):
    """Top-level `/api/v1/expense-attachments/{id}` — `DELETE` only
    (create/list are nested under `/expenses/{id}/attachment`, gated by
    `ExpensePermission` above). Gated by `expense.manage`, scoped to
    `obj.expense.project_id` (an `ExpenseAttachment` has no `project_id` of
    its own — it hangs off an `Expense`, which is the project-scoped parent)
    — same per-project 🟡 rule as `ExpensePermission`/`ProjectDocumentPermission`.
    """

    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False
        return user_has_permission_in_any_scope(user, EXPENSE_MANAGE)

    def has_object_permission(self, request, view, obj) -> bool:
        return user_has_permission(request.user, EXPENSE_MANAGE, project=obj.expense.project_id)


# --- ExpenseCategoryViewSet (`/api/v1/expense-categories`, list/retrieve) ---


class ExpenseCategoryPermission(BasePermission):
    """`apps.projects.api.ExpenseCategoryViewSet` (list/retrieve only, no
    writes here — see that class's own docstring).

    **Code-review finding (deliberately NOT `apps.catalog.permissions.
    TenantWideView`, which only ever checks a TENANT-WIDE grant,
    `project=None`):** `ExpenseCategory` is tenant-wide reference data with
    no `project_id` of its own — nothing to leak/scope against — so a PURE
    project-scoped Project Lead (only ever a project-scoped `project.view`
    membership, no tenant-wide grant; the exact persona the rest of this
    module is built around) must still be able to list it for the
    expense-form category picker / ledger name resolution. `TenantWideView`
    would 403 that user outright. `user_has_permission_in_any_scope` is the
    correct, permissive gate here: "holds `project.view` in ANY scope,
    tenant-wide OR project-scoped" — there is no per-project 🟡 check to
    layer on top afterwards (unlike `ProjectPermission`/`ExpensePermission`
    above) since the resource itself has no project to scope against.
    """

    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False
        return user_has_permission_in_any_scope(user, PROJECT_VIEW)

    def has_object_permission(self, request, view, obj) -> bool:
        return self.has_permission(request, view)
