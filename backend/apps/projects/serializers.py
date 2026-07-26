"""Serializers for the M7 project hub (`docs/tasks/M7-project-grants.md`).

Tenant-scoping note (R4/F1, same pattern as `apps.catalog.serializers`/
`apps.assets.serializers`): every writable relation that points at another
tenant-owned model (`Project.lead_user`, `Expense.category`, `Expense.asset`)
gets its queryset built in `get_fields()`, from that model's tenant-scoped
`.objects` manager, resolved lazily per-request — never a class-body
`queryset=Model.objects.all()` (which would evaluate the fail-closed manager
at import time with no tenant context and crash on app startup).
"""

from __future__ import annotations

from django.db.models import Q
from rest_framework import serializers

from apps.accounts.models import User
from apps.assets.models import Asset

from .models import Expense, ExpenseAttachment, ExpenseCategory, Project, ProjectDocument
from .services import budget_rollup


class ExpenseCategorySerializer(serializers.ModelSerializer):
    """Read-only reference data for `GET /api/v1/expense-categories`
    (frontend follow-up: the expense form's category picker needs
    name-to-id resolution, not just a raw numeric id). Modeled on
    `apps.catalog.serializers.TagSerializer` — no create/update/delete
    surface here; categories are seeded/managed by the data migration
    (`0006_seed_expense_categories`) and the `apps.projects.signals`
    per-tenant seed, not user-managed in this milestone.
    """

    class Meta:
        model = ExpenseCategory
        fields = ["id", "name", "is_active"]
        read_only_fields = fields


def _decimal_or_none(value) -> str | None:
    """Match `DecimalField`'s own string coercion (never a bare `float`) for
    values that bypass a real `DecimalField` on the way out (`budget_total`'s
    `to_representation` redaction below, the rollup `SerializerMethodField`s
    on `ProjectDetailSerializer`).
    """
    return None if value is None else str(value)


class ProjectSerializer(serializers.ModelSerializer):
    """List/create serializer — unchanged contract from the superseded
    `apps.catalog.serializers.ProjectSerializer` (same fields, same
    `(tenant, name)` uniqueness validation) so existing create/list clients
    and tests (`apps.catalog.tests.test_catalog_api`) keep working unchanged
    now that `apps.projects.api.ProjectViewSet` owns the `/projects` route.
    Adds the M7 grant-metadata fields as plain writable fields (all
    nullable/defaulted at the model layer, `apps.projects.models.Project`
    docstring) — `create`/`update` here stay reachable only via the
    Admin-only `create` action and the `project.manage`-gated `update`
    action respectively (`apps.projects.permissions.ProjectPermission`).

    **`budget_total` financial-fields gate (product decision, code-review
    finding #1: "financials and grant documents are restricted to that
    project's Lead (scoped) + Admins only" — supersedes the earlier "plain
    project metadata" reading of `docs/rbac.md`'s matrix footnote):**
    `budget_total` stays a normal WRITABLE `DecimalField` (Admin `create`,
    `project.manage`-gated `update` both still set it directly via `attrs`/
    `instance` — this gate is representation-only, `to_representation`
    never touches validation/save), but the OUTPUT is redacted to `null`
    for any caller who does not hold `expense.view` scoped to THIS project
    (a Member with no project-scoped Lead grant, or a Lead of a DIFFERENT
    project) — same per-project check `ProjectDetailSerializer` already uses
    for `spent`/`remaining`/`spend_by_category` below, applied here too so
    the base list/create serializer can never leak it either (list rows are
    still cheap: `_can_view_financials` is one small per-row RBAC lookup, no
    per-row N+1 against `Expense`, since it doesn't call `budget_rollup`).
    """

    class Meta:
        model = Project
        fields = [
            "id",
            "name",
            "code",
            "lead_user",
            "is_active",
            "funding_source",
            "sponsor",
            "start_date",
            "end_date",
            "budget_total",
            "currency",
            "status",
            "description",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]

    def get_fields(self):
        fields = super().get_fields()
        # R4/F1: `User.objects` (tenant-scoped), never `User.all_objects` —
        # see module docstring.
        fields["lead_user"].queryset = User.objects.all()  # type: ignore[attr-defined]
        return fields

    def _can_view_financials(self, project: Project) -> bool:
        """Shared by `budget_total`'s redaction here and
        `ProjectDetailSerializer`'s `spent`/`remaining`/`spend_by_category`:
        `expense.view` scoped to THIS SPECIFIC project (Lead-of-that-project,
        or Admin via a tenant-wide grant) — the exact per-project 🟡 rule
        `apps.projects.permissions` enforces at the request-gating layer,
        applied again here at the field level since `retrieve`/`list`
        themselves only ever require the broader, tenant-wide-grantable
        `project.view`.

        **Query-count fix (code-review finding):** for a LIST of N rows this
        used to call `user_has_permission(..., project=row.id)` once PER
        ROW — a fresh `Membership`/`Role`/`RolePermission`/`Permission`
        query every time. `apps.projects.api.ProjectViewSet.
        get_serializer_context` now resolves the caller's `expense.view`
        scope ONCE per request (`get_viewable_project_scope`) and hands it
        to every row via `context["financial_scope"]` — this method reads
        that precomputed `(tenant_wide, project_ids)` pair and does a plain
        in-memory set-membership check instead. Falls back to the
        old per-call query ONLY if a caller builds this serializer directly
        without going through the viewset (context missing the precomputed
        scope) — kept for robustness, not expected on the real request path.
        """
        scope = self.context.get("financial_scope")
        if scope is not None:
            tenant_wide, project_ids = scope
            return tenant_wide or project.id in project_ids

        from apps.rbac.permission_keys import EXPENSE_VIEW
        from apps.rbac.services import user_has_permission

        request = self.context.get("request")
        if request is None or not getattr(request, "user", None):
            return False
        return user_has_permission(request.user, EXPENSE_VIEW, project=project.id)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if "budget_total" in data and not self._can_view_financials(instance):
            data["budget_total"] = None
        return data

    def validate(self, attrs):
        # Carried `(tenant, name)` `UniqueConstraint`-without-serializer-check
        # finding from `apps.catalog.serializers.ProjectSerializer` — kept
        # here unchanged so `test_duplicate_project_name_is_400_not_500`
        # keeps passing verbatim.
        name = attrs.get("name", getattr(self.instance, "name", None))
        qs = Project.objects.filter(name=name)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)  # type: ignore[union-attr]
        if qs.exists():
            raise serializers.ValidationError({"name": "A project with this name already exists."})
        return attrs


class ProjectDetailSerializer(ProjectSerializer):
    """`GET/PATCH /api/v1/projects/{id}` — the M7 project hub detail, adding
    the computed budget rollup (`docs/tasks/M7-project-grants.md`
    "Endpoints"): `spent`, `remaining`, `spend_by_category`. Computed via
    `apps.projects.services.budget_rollup` (business logic kept out of the
    serializer per CLAUDE.md), called once in `to_representation` — a single
    aggregated query, no N+1 (see that function's own docstring).

    **Financial-fields gate (product decision, code-review finding #1:
    "financials and grant documents are restricted to that project's Lead
    (scoped) + Admins only"):** the LEDGER-DERIVED figures (`spent`,
    `remaining`, `spend_by_category`, computed from `Expense` rows) are
    gated behind `expense.view` scoped to THIS SPECIFIC project — same
    `_can_view_financials` check the base `ProjectSerializer` now also
    applies to `budget_total`. A caller without it (a Member with no
    project-scoped Lead grant, or a Lead of a DIFFERENT project) gets `null`
    for all four fields rather than a 403 — `retrieve` itself only ever
    required `project.view` (the object still exists and is nameable), so
    this is a field-level redaction, not a request-level denial.
    """

    spent = serializers.SerializerMethodField()
    remaining = serializers.SerializerMethodField()
    spend_by_category = serializers.SerializerMethodField()

    class Meta(ProjectSerializer.Meta):
        fields = ProjectSerializer.Meta.fields + ["spent", "remaining", "spend_by_category"]

    def _rollup(self, project: Project) -> dict:
        # Cached on the instance for the lifetime of one serialization pass
        # (3 `SerializerMethodField`s would otherwise re-run the same
        # aggregate query 3 times).
        cached = getattr(self, "_rollup_cache", None)
        if cached is None or cached[0] is not project:
            cached = (project, budget_rollup(project))
            self._rollup_cache = cached
        return cached[1]

    def get_spent(self, project: Project):
        if not self._can_view_financials(project):
            return None
        return _decimal_or_none(self._rollup(project)["spent"])

    def get_remaining(self, project: Project):
        if not self._can_view_financials(project):
            return None
        return _decimal_or_none(self._rollup(project)["remaining"])

    def get_spend_by_category(self, project: Project):
        if not self._can_view_financials(project):
            return None
        rows = self._rollup(project)["spend_by_category"]
        return [
            {
                "category_id": row["category_id"],
                "category": row["category"],
                "total": str(row["total"]),
            }
            for row in rows
        ]


class ExpenseAttachmentSerializer(serializers.ModelSerializer):
    """Read-only: created via the dedicated upload action
    (`POST /api/v1/expenses/{id}/attachment`), same pattern as
    `apps.assets.serializers.AttachmentSerializer`.
    """

    class Meta:
        model = ExpenseAttachment
        fields = [
            "id",
            "storage_key",
            "filename",
            "content_type",
            "size",
            "uploaded_by",
            "created_at",
        ]
        read_only_fields = fields


class ExpenseSerializer(serializers.ModelSerializer):
    attachments = ExpenseAttachmentSerializer(many=True, read_only=True)

    class Meta:
        model = Expense
        fields = [
            "id",
            "project",
            "category",
            "amount",
            "currency",
            "date",
            "vendor",
            "invoice_number",
            "description",
            "asset",
            "created_by",
            "attachments",
            "created_at",
            "updated_at",
        ]
        # `project` is derived from the URL (`/projects/{id}/expenses`, see
        # `apps.projects.api.ProjectViewSet.expenses`), never trusted from the
        # request body — same rule `apps.catalog.serializers.
        # CustomFieldDefSerializer` follows for its own URL-derived `category`.
        # `created_by` is set server-side from the request user (never
        # client-supplied, docs/tasks/M7-project-grants.md: "created_by set
        # server-side").
        read_only_fields = ["id", "project", "created_by", "created_at", "updated_at"]

    def get_fields(self):
        fields = super().get_fields()
        # R4/F1: every writable FK here is scoped to the tenant-scoped
        # manager, resolved lazily per-request (module docstring).
        fields["category"].queryset = ExpenseCategory.objects.all()  # type: ignore[attr-defined]

        # Code-review finding #4: `Asset.objects.all()` let a Lead of project
        # A link an expense to project B's asset (still tenant-scoped, so not
        # an R4 leak, but a cross-project data-integrity/scope violation — an
        # expense's `asset` should only ever be one of ITS OWN project's
        # assets, or a general-pool asset (`project_id IS NULL`), never
        # another project's). `project` comes from the URL-derived context
        # on create (`apps.projects.api.ProjectViewSet.expenses`, never
        # trusted from the request body — same rule as `category` above) or
        # from the existing instance's own `project` on update (`self.
        # instance` is already set by the time `get_fields()` runs for a
        # `PATCH`, see `apps.projects.api.ExpenseViewSet`). No project
        # resolvable at all (should be unreachable given both callers always
        # supply one) fails CLOSED to an empty queryset rather than falling
        # back to every asset.
        project = self.context.get("project") or getattr(self.instance, "project", None)
        if project is not None:
            fields["asset"].queryset = Asset.objects.filter(  # type: ignore[attr-defined]
                Q(project=project) | Q(project__isnull=True)
            )
        else:
            fields["asset"].queryset = Asset.objects.none()  # type: ignore[attr-defined]
        return fields


class ProjectDocumentSerializer(serializers.ModelSerializer):
    """Read/list serializer — creation goes through the dedicated multipart
    upload action (`POST /api/v1/projects/{id}/documents`), same pattern as
    `apps.assets.serializers.AttachmentSerializer`.
    """

    class Meta:
        model = ProjectDocument
        fields = [
            "id",
            "project",
            "kind",
            "storage_key",
            "filename",
            "content_type",
            "size",
            "uploaded_by",
            "created_at",
        ]
        read_only_fields = fields
