"""M7 project hub endpoints (`docs/tasks/M7-project-grants.md` "API"):
`GET/PATCH /projects/{id}` (+ budget rollup), `GET /projects/{id}/assets`,
`GET/POST /projects/{id}/expenses`, `GET/PATCH/DELETE /expenses/{id}`,
`POST/GET /expenses/{id}/attachment`, `GET/POST /projects/{id}/documents`,
`DELETE /documents/{id}`, `GET /projects/{id}/export.csv`.

**Route ownership (task instruction — avoid a conflicting double-registration
of `/api/v1/projects`):** `apps.projects.api.ProjectViewSet` (THIS module) is
now the SOLE viewset registered for the `"projects"` router prefix in
`config/urls.py` — it supersedes `apps.catalog.api.ProjectViewSet` (left in
place, unregistered, as the M7 task explicitly allows: "You may leave the
existing thin catalog ProjectViewSet"). `create`/`destroy`/the base list/
create field set are deliberately IDENTICAL to the superseded viewset's
contract (see `apps.projects.serializers.ProjectSerializer`'s own docstring)
so `apps.catalog.tests.test_catalog_api`'s existing `/api/v1/projects/`
create/list/RBAC tests keep passing unchanged; `retrieve`/`update` are the
NEW richer M7 surface (budget rollup, `project.manage` gating).

Tenant scoping (golden-path step 2): every `get_queryset()`/nested-action
queryset below is built from `Project.objects`/`Expense.objects`/
`ProjectDocument.objects`/`ExpenseAttachment.objects` (the tenant-scoped,
fail-closed manager), resolved per-request, never a class-level
`queryset = ...` (same reasoning as `apps.catalog.api`/`apps.assets.api`).

RBAC (golden-path step 3): `apps.projects.permissions` — see that module's
docstring for the exact per-project 🟡 scope rule and WHERE it's enforced
(`ProjectPermission.has_object_permission`/`ExpensePermission.
has_object_permission`/`ProjectDocumentPermission.has_object_permission` —
the ONE place a ProjectLead of project A is prevented from reaching project
B's expenses/budget/documents, the milestone's headline security property).

Audit (golden-path step 5): `docs/tasks/M7-project-grants.md`'s own
non-negotiable ("Every mutating action is audited") is broader than
`docs/rbac.md` §5's historical enumerated list — every Expense create/
update/delete, the project `create`/budget-grant `PATCH`/`destroy`, every
ProjectDocument create/delete, and every invoice attachment upload writes
an `AuditLog` entry here. `destroy` (code-review finding #3) captures the
FULL cascade `Project` delete destroys (`Expense`/`ExpenseAttachment`/
`ProjectDocument` are all `on_delete=CASCADE` off it) in the audit `before`,
not just the `Project` row itself — see `_project_cascade_snapshot`.

**Financial/document-read boundary (product decision, code-review findings
#1/#2 — read this before "fixing" it back):** `docs/rbac.md`'s matrix grants
`project.view` tenant-wide to Member/Viewer, but `budget_total`/`spent`/
`remaining`/`spend_by_category` (`apps.projects.serializers.
ProjectSerializer._can_view_financials`) and the ENTIRE `documents`
sub-resource (`apps.projects.permissions._action_permission_key`, reads
gated by `expense.view` rather than `project.view`) are restricted to that
project's Lead (scoped) + Admins only — i.e. `expense.view` scoped to the
SPECIFIC project, not merely "can see the project exists". A plain Member's
`GET /projects/{id}` still 200s (the row itself is visible, matching
`project.view`'s tenant-wide grant) but with those fields `null`; a plain
Member's `GET /projects/{id}/documents` 403s outright (list/create is a
sub-resource action, not a bare field on an otherwise-visible row). This is
intentional, not a regression — proposals/contracts routinely restate the
exact budget figures being redacted elsewhere.
"""

from __future__ import annotations

import csv
import logging
from decimal import Decimal

import django_filters as filters
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction
from django.http import StreamingHttpResponse
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.filters import OrderingFilter, SearchFilter
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from apps.assets.api import AssetFilterSet, AssetSearchFilter, visible_assets_queryset
from apps.assets.models import Attachment
from apps.assets.serializers import AssetSerializer
from apps.assets.services import validate_attachment_upload
from apps.audit.services import client_ip, write_audit_log
from apps.common.errors import problem_response
from apps.common.pagination import BoundedPageNumberPagination
from apps.jobs.models import Job
from apps.jobs.serializers import JobSerializer
from apps.rbac.permission_keys import (
    ASSET_VIEW,
    EXPENSE_MANAGE,
    EXPENSE_VIEW,
    PROJECT_MANAGE,
    TENANT_MANAGE,
)
from apps.rbac.services import get_viewable_project_scope, user_has_permission
from apps.tenancy.context import tenant_context

from .models import Expense, ExpenseAttachment, ExpenseCategory, Project, ProjectDocument
from .permissions import (
    ExpenseAttachmentPermission,
    ExpenseCategoryPermission,
    ExpensePermission,
    ProjectDocumentPermission,
    ProjectPermission,
    project_list_queryset_scope,
)
from .serializers import (
    ExpenseAttachmentSerializer,
    ExpenseCategorySerializer,
    ExpenseSerializer,
    ProjectDetailSerializer,
    ProjectDocumentSerializer,
    ProjectSerializer,
)
from .services import save_expense_attachment_file, save_project_document_file
from .tasks import generate_project_archive_zip, generate_project_report_pdf

logger = logging.getLogger(__name__)


class _EchoWriter:
    """A file-like object whose `write()` just returns what it was given —
    lets `csv.writer` yield each formatted line straight into a
    `StreamingHttpResponse` generator instead of buffering the whole file.
    Same technique as `apps.imports.exports._EchoWriter`.
    """

    def write(self, value):
        return value


# Writable-field snapshot for the audit before/after on `PATCH /projects/{id}`
# (docs/tasks/M7-project-grants.md's budget/grant metadata surface).
_PROJECT_AUDIT_FIELDS = [
    "name",
    "code",
    "lead_user_id",
    "is_active",
    "funding_source",
    "sponsor",
    "start_date",
    "end_date",
    "budget_total",
    "currency",
    "status",
    "description",
]


def _project_audit_snapshot(project: Project) -> dict:
    snapshot = {}
    for field in _PROJECT_AUDIT_FIELDS:
        value = getattr(project, field)
        # Decimal/date aren't JSON-serializable as-is; AuditLog.before/after
        # is JSONB (see apps.audit.models) — stringify non-primitive types.
        if isinstance(value, Decimal):
            value = str(value)
        elif hasattr(value, "isoformat"):
            value = value.isoformat()
        snapshot[field] = value
    return snapshot


def _project_cascade_snapshot(project: Project) -> dict:
    """Code-review finding #3: `Project` is a full `ModelViewSet` with
    `Expense`/`ExpenseAttachment`/`ProjectDocument` all `on_delete=CASCADE`
    off it (`apps.projects.models`) — deleting a project silently wipes
    every expense, invoice scan, and grant document with NOTHING in
    `AuditLog` to show what was destroyed. Captures the FULL cascade (not
    just the `Project` row) BEFORE `instance.delete()` runs, so the audit
    `before` is actually useful for reconstructing what an Admin destroyed.
    `ExpenseAttachment` rows are counted only (not itemized) — their own
    content (`storage_key`/`filename`) is already itemizable per-expense via
    `expenses` here if ever needed, and the byte count would otherwise bloat
    this JSONB row for a project with many receipts.
    """
    expenses = [
        {
            "id": row["id"],
            "amount": str(row["amount"]),
            "date": row["date"].isoformat(),
            "vendor": row["vendor"],
            "category_id": row["category_id"],
        }
        for row in Expense.objects.filter(project=project).values(
            "id", "amount", "date", "vendor", "category_id"
        )
    ]
    documents = [
        {"id": row["id"], "kind": row["kind"], "filename": row["filename"]}
        for row in ProjectDocument.objects.filter(project=project).values("id", "kind", "filename")
    ]
    attachment_count = ExpenseAttachment.objects.filter(expense__project=project).count()
    return {
        "project": _project_audit_snapshot(project),
        "cascaded_expenses": expenses,
        "cascaded_expense_attachment_count": attachment_count,
        "cascaded_documents": documents,
    }


class ExpenseCategoryViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    """`GET /api/v1/expense-categories` (+ retrieve) — read-only tenant-wide
    reference data (id/name/is_active) for the expense form's category
    picker (frontend follow-up: the form was stuck passing a raw numeric
    id with no way to resolve/display names). Modeled EXACTLY on
    `apps.catalog.api.TagViewSet`: no create/update/delete surface —
    `ExpenseCategory` rows are seeded/managed by the data migration
    (`0006_seed_expense_categories`) and `apps.projects.signals`'
    per-tenant seed, not user-managed in this milestone.

    **Permission**: `apps.projects.permissions.ExpenseCategoryPermission`
    (code-review finding — NOT `apps.catalog.permissions.TenantWideView`,
    which only ever checks a TENANT-WIDE grant: a pure project-scoped
    Project Lead, no tenant-wide membership at all, would 403 outright and
    lose the category picker). `ExpenseCategory` is tenant-wide reference
    data with no `project_id` of its own (unlike `Expense`/
    `ProjectDocument`), so there is nothing to scope 🟡-style against —
    anyone who holds `project.view` in ANY scope (tenant-wide OR
    project-scoped) can read this non-financial reference list, same tier
    as tags/locations. Deliberately NOT gated by `expense.view` (the
    financial-boundary key `apps.projects.permissions` uses for
    `spent`/`remaining`/`spend_by_category`/documents) — a bare category
    NAME is not itself financial data.

    `?include_inactive=true` opts into also returning retired categories
    (`is_active=False`, kept for historical `Expense.category` FK
    integrity per `apps.projects.models.ExpenseCategory`'s own docstring);
    the default excludes them, matching what a picker should offer.
    """

    serializer_class = ExpenseCategorySerializer
    permission_classes = [ExpenseCategoryPermission]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    search_fields = ["name"]
    ordering_fields = ["name"]
    ordering = ["name"]
    pagination_class = BoundedPageNumberPagination

    def get_queryset(self):
        # Tenant-scoped manager, resolved per-request (see module docstring
        # of apps.catalog.api for why this can't be a class-level `queryset`).
        qs = ExpenseCategory.objects.all()
        include_inactive = self.request.query_params.get("include_inactive", "").lower() == "true"
        if not include_inactive:
            qs = qs.filter(is_active=True)
        return qs


class ExpenseFilterSet(filters.FilterSet):
    """`?category=`/`?date_from=`/`?date_to=` for the nested
    `/projects/{id}/expenses` list (`docs/tasks/M7-project-grants.md`:
    "Paginated, filterable by category/date"). `project` itself is NOT a
    filter here — it's fixed by the URL, applied directly in
    `ProjectViewSet.expenses` before this filterset ever runs.
    """

    category = filters.NumberFilter(field_name="category_id")
    date_from = filters.DateFilter(field_name="date", lookup_expr="gte")
    date_to = filters.DateFilter(field_name="date", lookup_expr="lte")

    class Meta:
        model = Expense
        fields = ["category", "date_from", "date_to"]


class ProjectViewSet(viewsets.ModelViewSet):
    permission_classes = [ProjectPermission]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ["is_active", "status"]
    search_fields = ["name", "code"]
    ordering_fields = ["name", "created_at"]
    ordering = ["name"]
    pagination_class = BoundedPageNumberPagination

    def get_serializer_class(self):
        if self.action in ("retrieve", "update", "partial_update"):
            return ProjectDetailSerializer
        return ProjectSerializer

    def get_serializer_context(self):
        # Code-review finding: `ProjectSerializer._can_view_financials` used
        # to call `user_has_permission(..., project=row.id)` PER ROW — each
        # call re-queries `Membership`/`Role`/`RolePermission`/`Permission`
        # from scratch, so `GET /projects` fired up to one such query set
        # PER PAGE ROW (a real N+1). Resolve the caller's `expense.view`
        # scope ONCE per request here (`get_viewable_project_scope`, the
        # SAME helper `project_list_queryset_scope`/`apps.assets.api.
        # visible_assets_queryset` already use for exactly this
        # "resolve-once, reuse-per-row" pattern) and hand it to every row's
        # serializer via `context["financial_scope"]` — each row then does
        # an in-memory set-membership check instead of a fresh query.
        context = super().get_serializer_context()
        user = getattr(self.request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False):
            context["financial_scope"] = get_viewable_project_scope(user, EXPENSE_VIEW)
        return context

    def get_queryset(self):
        qs = Project.objects.select_related("lead_user")
        if self.action == "list":
            # Row-level restriction (docs/rbac.md §1 union-of-memberships),
            # same pattern as `apps.assets.api.visible_assets_queryset` — a
            # pure ProjectLead (project-scoped `project.view` only) sees
            # exactly their own project(s), never another tenant's or
            # another Lead's project.
            tenant_wide, project_ids = project_list_queryset_scope(self.request.user)
            if not tenant_wide:
                qs = qs.filter(id__in=project_ids) if project_ids else qs.none()
        return qs

    def perform_create(self, serializer):
        # Tenant is derived from the authenticated session, never the client
        # (R4) — same as `apps.catalog.api.ProjectViewSet.perform_create`.
        project = serializer.save(tenant=self.request.user.tenant)  # type: ignore[union-attr]
        # Code-review finding #3: audit every mutating action on this
        # resource, not just `update`/`destroy` — `action=TENANT_MANAGE`
        # matches the permission actually enforced for `create` (Admin-only
        # structural CRUD, `apps.projects.permissions.PROJECT_ACTION_PERMISSION_MAP`).
        write_audit_log(
            tenant_id=project.tenant_id,
            actor=self.request.user,
            action=TENANT_MANAGE,
            entity_type="project",
            entity_id=project.id,
            before=None,
            after=_project_audit_snapshot(project),
            ip=client_ip(self.request),
        )

    def perform_destroy(self, instance):
        # Code-review finding #3: `Expense`/`ExpenseAttachment`/
        # `ProjectDocument` are all `on_delete=CASCADE` off `Project` — this
        # single delete silently destroys every expense/invoice-scan/
        # document row too. Capture the FULL cascade (see
        # `_project_cascade_snapshot`) BEFORE deleting, so the audit
        # `before` actually shows what was destroyed, not just the empty
        # `Project` row.
        before = _project_cascade_snapshot(instance)
        tenant_id = instance.tenant_id
        project_id = instance.id
        instance.delete()
        write_audit_log(
            tenant_id=tenant_id,
            actor=self.request.user,
            action=TENANT_MANAGE,
            entity_type="project",
            entity_id=project_id,
            before=before,
            after=None,
            ip=client_ip(self.request),
        )

    def perform_update(self, serializer):
        project = serializer.instance
        before = _project_audit_snapshot(project)
        serializer.save()
        after = _project_audit_snapshot(serializer.instance)
        if before != after:
            write_audit_log(
                tenant_id=project.tenant_id,
                actor=self.request.user,
                action=PROJECT_MANAGE,
                entity_type="project",
                entity_id=project.id,
                before=before,
                after=after,
                ip=client_ip(self.request),
            )

    @action(detail=True, methods=["get"], url_path="assets")
    def assets(self, request, pk=None):
        """`GET /api/v1/projects/{id}/assets` — reuses `apps.assets.api.
        visible_assets_queryset` filtered to THIS project, same pagination +
        serializer + filters as `GET /api/v1/assets`. `get_object()` below
        re-scopes `pk` through the tenant-scoped `Project` queryset AND
        re-checks object-level `project.view` (the per-project 🟡 check,
        `apps.projects.permissions.ProjectPermission.has_object_permission`)
        — a guessed/cross-tenant/other-Lead's project id 404s/403s here,
        never leaks an asset list.
        """
        project = self.get_object()
        queryset = visible_assets_queryset(request, ASSET_VIEW).filter(project_id=project.id)

        # Same filter/search/ordering as `GET /api/v1/assets` (task spec:
        # "same pagination + asset serializer as the list"), applied
        # EXPLICITLY here rather than via `self.filter_backends`/
        # `self.ordering_fields` — those class attributes belong to
        # `ProjectViewSet`'s OWN list endpoint (different field set), so
        # reusing DRF's automatic `filter_queryset()` pipeline would
        # validate `?ordering=` against the wrong whitelist.
        queryset = AssetFilterSet(request.query_params, queryset=queryset, request=request).qs
        queryset = AssetSearchFilter().filter_queryset(request, queryset, self)
        ordering = request.query_params.get("ordering", "")
        if ordering:
            allowed = {"name", "created_at", "status", "purchase_date"}
            order_fields = [seg for seg in ordering.split(",") if seg.lstrip("-") in allowed]
            if order_fields:
                queryset = queryset.order_by(*order_fields)

        page = self.paginate_queryset(queryset)
        serializer = AssetSerializer(page if page is not None else queryset, many=True)
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)

    @action(detail=True, methods=["get", "post"], url_path="expenses")
    def expenses(self, request, pk=None):
        """`GET/POST /api/v1/projects/{id}/expenses` — `GET` gated by
        `expense.view`, `POST` by `expense.manage`
        (`apps.projects.permissions._action_permission_key`), both scoped to
        THIS project via `get_object()` below. `category`/`asset` (if
        supplied) are re-scoped through the tenant-scoped querysets in
        `ExpenseSerializer.get_fields()` — never trusted from the client
        beyond that (R4). `project` is passed via `context` (code-review
        finding #4) so `ExpenseSerializer.get_fields()` can scope the
        selectable `asset` queryset to THIS project + the general pool,
        never another project's assets.
        """
        project = self.get_object()

        if request.method.upper() == "POST":
            serializer = ExpenseSerializer(
                data=request.data, context={"request": request, "project": project}
            )
            serializer.is_valid(raise_exception=True)
            expense = serializer.save(
                tenant=request.user.tenant, project=project, created_by=request.user
            )
            write_audit_log(
                tenant_id=project.tenant_id,
                actor=request.user,
                action=EXPENSE_MANAGE,
                entity_type="expense",
                entity_id=expense.id,
                before=None,
                after=ExpenseSerializer(expense).data,
                ip=client_ip(request),
            )
            return Response(ExpenseSerializer(expense).data, status=status.HTTP_201_CREATED)

        queryset = (
            Expense.objects.filter(project=project)
            .select_related("category", "asset", "created_by")
            .prefetch_related("attachments")
        )
        queryset = ExpenseFilterSet(request.query_params, queryset=queryset, request=request).qs

        page = self.paginate_queryset(queryset)
        serializer = ExpenseSerializer(page if page is not None else queryset, many=True)
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)

    @action(detail=True, methods=["get", "post"], url_path="documents")
    def documents(self, request, pk=None):
        """`GET/POST /api/v1/projects/{id}/documents` — `GET` gated by
        `expense.view` (product decision, code-review finding #2:
        proposals/contracts routinely restate exact budget figures, so
        document reads get the SAME project-scoped financial boundary as
        expenses — NOT the tenant-wide-grantable `project.view` the
        original spec text used), `POST` by `project.manage`, both scoped
        to THIS project (`apps.projects.permissions._action_permission_key`).
        `POST` is a multipart upload (`file` + `kind`), same
        validate-then-write pattern as `apps.assets.api.AssetViewSet.
        attachments`: `validate_attachment_upload` runs BEFORE any bytes hit
        the storage backend, and only `storage_key` + metadata is ever
        persisted (`apps.projects.services.save_project_document_file`).
        """
        project = self.get_object()

        if request.method.upper() == "POST":
            uploaded_file = request.FILES.get("file")
            if uploaded_file is None:
                return problem_response(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    title="Missing file",
                    detail="A multipart 'file' field is required.",
                )
            kind = request.data.get("kind", ProjectDocument.Kind.OTHER)
            if kind not in ProjectDocument.Kind.values:
                return problem_response(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    title="Invalid kind",
                    detail=f"'kind' must be one of {ProjectDocument.Kind.values}.",
                )
            file_kind = request.data.get("file_kind", "doc")
            if file_kind not in ("photo", "doc"):
                return problem_response(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    title="Invalid file_kind",
                    detail="'file_kind' must be 'photo' or 'doc'.",
                )
            validate_attachment_upload(kind=file_kind, uploaded_file=uploaded_file)

            storage_key, content_type, size = save_project_document_file(
                tenant_id=project.tenant_id, project_id=project.id, uploaded_file=uploaded_file
            )
            document = ProjectDocument.objects.create(
                tenant=project.tenant,
                project=project,
                kind=kind,
                storage_key=storage_key,
                filename=uploaded_file.name,
                content_type=content_type,
                size=size,
                uploaded_by=request.user,
            )
            write_audit_log(
                tenant_id=project.tenant_id,
                actor=request.user,
                action=PROJECT_MANAGE,
                entity_type="project_document",
                entity_id=document.id,
                before=None,
                after=ProjectDocumentSerializer(document).data,
                ip=client_ip(request),
            )
            return Response(
                ProjectDocumentSerializer(document).data, status=status.HTTP_201_CREATED
            )

        queryset = ProjectDocument.objects.filter(project=project).select_related("uploaded_by")
        page = self.paginate_queryset(queryset)
        serializer = ProjectDocumentSerializer(page if page is not None else queryset, many=True)
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)

    @action(detail=True, methods=["get"], url_path=r"export\.csv")
    def export_csv(self, request, pk=None):
        """`GET /api/v1/projects/{id}/export.csv?fields=...` — streamed,
        field-selectable export of this project's expense ledger, RBAC-scoped
        the same way as the endpoint's `GET` (`expense.view`). Models
        `apps.imports.exports.AssetExportView`'s `StreamingHttpResponse` +
        `_EchoWriter` technique exactly (same module docstring reasoning:
        cheap per-row CSV writes, no Celery job needed at this scale).
        """
        # `get_object()` already ran `has_object_permission` with
        # `view.action == "export_csv"` -> `EXPENSE_VIEW`, scoped to THIS
        # project (`apps.projects.permissions.PROJECT_ACTION_PERMISSION_MAP`)
        # — no separate manual check needed here.
        project = self.get_object()

        all_fields = [
            "date",
            "category",
            "vendor",
            "invoice_number",
            "amount",
            "currency",
            "description",
            "asset",
        ]
        requested = request.query_params.get("fields")
        if requested:
            fields = [f for f in requested.split(",") if f in all_fields]
            if not fields:
                fields = all_fields
        else:
            fields = all_fields

        queryset = (
            Expense.objects.filter(project=project)
            .select_related("category", "asset")
            .order_by("-date", "-id")
        )
        tenant_id = request.user.tenant_id

        def rows():
            with tenant_context(tenant_id):
                writer = csv.writer(_EchoWriter())
                yield writer.writerow(fields)
                for expense in queryset.iterator(chunk_size=200):
                    row_values = {
                        "date": expense.date.isoformat(),
                        "category": expense.category.name if expense.category else "",
                        "vendor": expense.vendor,
                        "invoice_number": expense.invoice_number,
                        "amount": str(expense.amount),
                        "currency": expense.currency,
                        "description": expense.description,
                        "asset": expense.asset.name if expense.asset else "",
                    }
                    yield writer.writerow([row_values[f] for f in fields])

        response = StreamingHttpResponse(rows(), content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="project-expenses.csv"'
        return response

    @action(detail=True, methods=["post"], url_path="report")
    def report(self, request, pk=None):
        """`POST /api/v1/projects/{id}/report` (Slice 3, `pwa-scan-specialist`)
        — enqueues a Celery job that renders the structured project audit
        report PDF (`apps.projects.report`/`apps.projects.tasks.
        generate_project_report_pdf`), reusing the exact same async-job
        poller pattern `apps.labels.api.LabelGenerateView` established for
        label PDFs: create a `Job` row (status=queued) INSIDE this request's
        transaction, dispatch the task via `transaction.on_commit(...)` so
        it can never run against a not-yet-committed `Job`, and return the
        `Job` for the client to poll (`GET /api/v1/jobs/{id}`) until
        `download_url` is populated.

        Accepts an optional JSON body `{"include_invoice_scans": bool,
        "include_project_documents": bool}` (both default `False`).
        `include_invoice_scans` is opt-in embedding of rasterized invoice/
        receipt scan previews in the report appendix (see `apps.projects.
        services.resolve_project_report_data`'s docstring); left off, the
        appendix stays filename-only, identical to the report's behavior
        before this option existed. Embedding scans bloats the PDF, so it is
        deliberately never the default.

        `include_project_documents` is opt-in appending of each project
        document's FULL PAGES (not a thumbnail) onto the end of the rendered
        report — see `apps.projects.report._append_project_documents`'s
        docstring for exactly how. Also deliberately never the default: it
        can add a lot of pages and reads/rasterizes every document on file.

        Gated on `expense.view` scoped to THIS project
        (`apps.projects.permissions.PROJECT_ACTION_PERMISSION_MAP["report"]`)
        — the report inlines the SAME budget/spend-by-category/itemized-
        ledger figures the financial redaction boundary already restricts
        elsewhere (`apps.projects.serializers.ProjectDetailSerializer.
        _can_view_financials`, this module's own docstring "Financial/
        document-read boundary"), so a caller who can't see a single expense
        row here must not be able to generate a PDF containing all of them.
        `get_object()` below re-runs that exact object-level check.
        """
        project = self.get_object()
        include_invoice_scans = bool(request.data.get("include_invoice_scans", False))
        include_project_documents = bool(request.data.get("include_project_documents", False))

        job = Job.objects.create(
            tenant=request.user.tenant,
            job_type="project_report_pdf",
            params={
                "project_id": project.id,
                "include_invoice_scans": include_invoice_scans,
                "include_project_documents": include_project_documents,
            },
            created_by=request.user,
        )
        # Audit the report-generation request itself (task instruction:
        # "Audit the report generation request") — a financial-document
        # export is worth an immutable trail even though it isn't a mutation
        # of `Project`/`Expense` state, same "record it anyway" posture
        # `export_csv`'s sibling M6 import/export jobs take.
        write_audit_log(
            tenant_id=project.tenant_id,
            actor=request.user,
            action=EXPENSE_VIEW,
            entity_type="project_report",
            entity_id=project.id,
            before=None,
            after={"job_id": str(job.id)},
            ip=client_ip(request),
        )

        transaction.on_commit(
            lambda: generate_project_report_pdf.delay(
                job_id=str(job.id),
                tenant_id=job.tenant_id,
                project_id=project.id,
                include_invoice_scans=include_invoice_scans,
                include_project_documents=include_project_documents,
            )
        )

        return Response(JobSerializer(job).data, status=status.HTTP_202_ACCEPTED)

    @action(detail=True, methods=["post"], url_path="archive")
    def archive(self, request, pk=None):
        """`POST /api/v1/projects/{id}/archive` — enqueues a Celery job that
        bundles this project's ORIGINAL files into one structured ZIP
        (`apps.projects.archive`), for local storage or handing to an
        auditor. Same async-job contract as `report`/`labels/generate`: a
        `Job` row created inside this transaction, dispatched on commit,
        polled at `GET /api/v1/jobs/{id}` until `download_url` appears.

        Deliberately NOT the report PDF: that RENDERS (and rasterizes) the
        financial record into one document; this ships the source files
        byte-for-byte with a `manifest.csv`, which is what an audit actually
        asks for.

        Optional JSON body — `{"include_documents": bool (default true),
        "include_invoices": bool (default true), "include_asset_attachments":
        bool (default false)}`. Asset attachments default OFF because they
        are the one section that can be arbitrarily large (every photo of
        every asset on the project) and are usually not what an auditor
        wants; the archive has a hard size cap
        (`apps.projects.archive.MAX_ARCHIVE_BYTES`) that fails the job with
        an actionable message rather than exhausting the worker.

        Gated on `expense.view` scoped to THIS project — the bundle contains
        the original invoice scans and project documents, i.e. strictly more
        financial material than the report (`PROJECT_ACTION_PERMISSION_MAP
        ["archive"]`, and `get_object()` re-runs the object-level check).
        """
        project = self.get_object()

        def _flag(name: str, default: bool) -> bool:
            value = request.data.get(name, default)
            return bool(value) if not isinstance(value, str) else value.lower() == "true"

        include_documents = _flag("include_documents", True)
        include_invoices = _flag("include_invoices", True)
        include_asset_attachments = _flag("include_asset_attachments", False)

        job = Job.objects.create(
            tenant=request.user.tenant,
            job_type="project_archive_zip",
            params={
                "project_id": project.id,
                "include_documents": include_documents,
                "include_invoices": include_invoices,
                "include_asset_attachments": include_asset_attachments,
            },
            created_by=request.user,
        )
        # Same "record the export itself" posture as `report` above — pulling
        # every invoice scan for a grant-funded project off the system is
        # exactly the event an audit trail exists for.
        write_audit_log(
            tenant_id=project.tenant_id,
            actor=request.user,
            action=EXPENSE_VIEW,
            entity_type="project_archive",
            entity_id=project.id,
            before=None,
            after={"job_id": str(job.id), "params": job.params},
            ip=client_ip(request),
        )

        transaction.on_commit(
            lambda: generate_project_archive_zip.delay(
                job_id=str(job.id),
                tenant_id=job.tenant_id,
                project_id=project.id,
                include_documents=include_documents,
                include_invoices=include_invoices,
                include_asset_attachments=include_asset_attachments,
                generated_by=request.user.email,
            )
        )

        return Response(JobSerializer(job).data, status=status.HTTP_202_ACCEPTED)


class ExpenseViewSet(
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """`/api/v1/expenses/{id}` — no `list`/`create` at this top level (both
    live nested under `/projects/{id}/expenses`, `ProjectViewSet.expenses`
    above) so a client can never list/create expenses without a project
    context, matching the M7 spec's route list exactly.
    """

    serializer_class = ExpenseSerializer
    permission_classes = [ExpensePermission]
    http_method_names = ["get", "patch", "delete", "post", "head", "options"]

    def get_queryset(self):
        return Expense.objects.select_related("project", "category", "asset").prefetch_related(
            "attachments"
        )

    def perform_update(self, serializer):
        expense = serializer.instance
        before = ExpenseSerializer(expense).data
        serializer.save()
        write_audit_log(
            tenant_id=expense.tenant_id,
            actor=self.request.user,
            action=EXPENSE_MANAGE,
            entity_type="expense",
            entity_id=expense.id,
            before=before,
            after=ExpenseSerializer(serializer.instance).data,
            ip=client_ip(self.request),
        )

    def perform_destroy(self, instance):
        before = ExpenseSerializer(instance).data
        tenant_id = instance.tenant_id
        expense_id = instance.id
        instance.delete()
        write_audit_log(
            tenant_id=tenant_id,
            actor=self.request.user,
            action=EXPENSE_MANAGE,
            entity_type="expense",
            entity_id=expense_id,
            before=before,
            after=None,
            ip=client_ip(self.request),
        )

    @action(
        detail=True,
        methods=["get", "post"],
        url_path="attachment",
        parser_classes=[MultiPartParser, FormParser],
    )
    def attachment(self, request, pk=None):
        """`POST /api/v1/expenses/{id}/attachment` (invoice scan upload,
        `expense.manage`) / `GET` (list this expense's scans, `expense.view`)
        — same validate-then-write pattern as `apps.assets.api.AssetViewSet.
        attachments`, reusing `apps.assets.services.
        validate_attachment_upload`/`apps.projects.services.
        save_expense_attachment_file` (itself a thin wrapper around the ONE
        shared `save_attachment_file` writer).
        """
        expense = self.get_object()  # tenant + object-level RBAC already enforced

        if request.method.upper() == "GET":
            attachments = expense.attachments.all()
            return Response(ExpenseAttachmentSerializer(attachments, many=True).data)

        uploaded_file = request.FILES.get("file")
        if uploaded_file is None:
            return problem_response(
                status_code=status.HTTP_400_BAD_REQUEST,
                title="Missing file",
                detail="A multipart 'file' field is required.",
            )
        kind = request.data.get("kind", "doc")
        if kind not in ("photo", "doc"):
            return problem_response(
                status_code=status.HTTP_400_BAD_REQUEST,
                title="Invalid kind",
                detail="'kind' must be 'photo' or 'doc'.",
            )
        validate_attachment_upload(kind=kind, uploaded_file=uploaded_file)

        storage_key, content_type, size = save_expense_attachment_file(
            tenant_id=expense.tenant_id, expense_id=expense.id, uploaded_file=uploaded_file
        )
        attachment = ExpenseAttachment.objects.create(
            tenant=expense.tenant,
            expense=expense,
            storage_key=storage_key,
            filename=uploaded_file.name,
            content_type=content_type,
            size=size,
            uploaded_by=request.user,
        )
        write_audit_log(
            tenant_id=expense.tenant_id,
            actor=request.user,
            action=EXPENSE_MANAGE,
            entity_type="expense_attachment",
            entity_id=attachment.id,
            before=None,
            after=ExpenseAttachmentSerializer(attachment).data,
            ip=client_ip(request),
        )
        return Response(
            ExpenseAttachmentSerializer(attachment).data, status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=["post"], url_path="attachment-from-asset")
    def attachment_from_asset(self, request, pk=None):
        """`POST /api/v1/expenses/{id}/attachment-from-asset`
        `{"attachment": <asset attachment id>}` — copy a PO/invoice already
        filed against an ASSET onto this expense, so the "fetch from asset"
        flow (`GET /assets/{id}/expense-prefill`) can bring the paperwork
        across too instead of asking someone to download-then-re-upload.

        A real COPY, not a shared reference: the new `ExpenseAttachment` owns
        its own storage object under `expense-attachments/`, so deleting the
        asset (or its attachment) later can never punch a hole in the
        financial record an auditor is holding.

        Two authorizations, both required and both re-checked here:
        - `expense.manage` scoped to this expense's project — already enforced
          by `get_object()` via `ExpensePermission` (the same gate as the
          ordinary upload above);
        - `asset.view` scoped to the SOURCE asset's project — checked
          explicitly below, because the source lives outside this expense's
          project entirely. Without it, a lead of project A could pull a
          document off an asset belonging to project B by id.
        """
        expense = self.get_object()  # tenant + object-level RBAC (expense.manage)

        try:
            attachment_id = int(request.data.get("attachment"))
        except (TypeError, ValueError):
            return problem_response(
                status_code=status.HTTP_400_BAD_REQUEST,
                title="Missing attachment",
                detail="An 'attachment' id (an asset attachment) is required.",
            )

        # Tenant-scoped manager: a cross-tenant id simply doesn't resolve.
        source = Attachment.objects.filter(pk=attachment_id).select_related("asset").first()
        if source is None:
            return problem_response(
                status_code=status.HTTP_404_NOT_FOUND,
                title="Not found",
                detail="No such asset attachment.",
            )
        if not user_has_permission(request.user, ASSET_VIEW, project=source.asset.project_id):
            # 404, not 403: a caller who cannot see the source asset must not
            # learn that this attachment id exists — same existence-leak
            # posture as `apps.assets.api.AssetResolveView`.
            return problem_response(
                status_code=status.HTTP_404_NOT_FOUND,
                title="Not found",
                detail="No such asset attachment.",
            )

        try:
            with default_storage.open(source.storage_key, "rb") as fh:
                content = ContentFile(fh.read(), name=source.filename)
        except Exception:
            return problem_response(
                status_code=status.HTTP_400_BAD_REQUEST,
                title="Source file unavailable",
                detail="The asset's file could not be read from storage.",
            )

        storage_key, content_type, size = save_expense_attachment_file(
            tenant_id=expense.tenant_id, expense_id=expense.id, uploaded_file=content
        )
        attachment = ExpenseAttachment.objects.create(
            tenant=expense.tenant,
            expense=expense,
            storage_key=storage_key,
            filename=source.filename,
            content_type=content_type or source.content_type,
            size=size,
            uploaded_by=request.user,
        )
        write_audit_log(
            tenant_id=expense.tenant_id,
            actor=request.user,
            action=EXPENSE_MANAGE,
            entity_type="expense_attachment",
            entity_id=attachment.id,
            before=None,
            after={
                **ExpenseAttachmentSerializer(attachment).data,
                "copied_from_asset_attachment": source.id,
                "copied_from_asset": source.asset_id,
            },
            ip=client_ip(request),
        )
        return Response(
            ExpenseAttachmentSerializer(attachment).data, status=status.HTTP_201_CREATED
        )


class ExpenseAttachmentViewSet(mixins.DestroyModelMixin, viewsets.GenericViewSet):
    """`/api/v1/expense-attachments/{id}` — `DELETE` only (list/create are
    nested under `/expenses/{id}/attachment`, `ExpenseViewSet.attachment`
    above). Mirrors `ProjectDocumentViewSet` exactly.
    """

    serializer_class = ExpenseAttachmentSerializer
    permission_classes = [ExpenseAttachmentPermission]
    http_method_names = ["delete", "head", "options"]

    def get_queryset(self):
        return ExpenseAttachment.objects.select_related(
            "expense", "expense__project", "uploaded_by"
        )

    def perform_destroy(self, instance):
        before = ExpenseAttachmentSerializer(instance).data
        tenant_id = instance.tenant_id
        attachment_id = instance.id
        storage_key = instance.storage_key
        instance.delete()
        # Deliberate divergence from `ProjectDocumentViewSet.perform_destroy`
        # (which only deletes the DB row and leaves the file orphaned on
        # disk/object storage): the user explicitly wants disk space reclaimed
        # for this new delete path. `save_expense_attachment_file`/
        # `save_attachment_file` write via `default_storage.save(...)`, so
        # `default_storage.delete()` is the symmetric call — works against
        # either the local filesystem or an S3-compatible backend per the
        # django-storages config, never a hardcoded path join. Best-effort:
        # if the file is already missing (or the backend errors), that must
        # not roll back / block the DB delete that already happened above.
        try:
            default_storage.delete(storage_key)
        except Exception:
            logger.warning(
                "Failed to delete storage object %r for expense_attachment %s "
                "(tenant %s) after DB row delete; DB delete already committed.",
                storage_key,
                attachment_id,
                tenant_id,
                exc_info=True,
            )
        write_audit_log(
            tenant_id=tenant_id,
            actor=self.request.user,
            action=EXPENSE_MANAGE,
            entity_type="expense_attachment",
            entity_id=attachment_id,
            before=before,
            after=None,
            ip=client_ip(self.request),
        )


class ProjectDocumentViewSet(mixins.DestroyModelMixin, viewsets.GenericViewSet):
    """`/api/v1/documents/{id}` — `DELETE` only (list/create are nested
    under `/projects/{id}/documents`, `ProjectViewSet.documents` above).
    """

    serializer_class = ProjectDocumentSerializer
    permission_classes = [ProjectDocumentPermission]
    http_method_names = ["delete", "head", "options"]

    def get_queryset(self):
        return ProjectDocument.objects.select_related("project", "uploaded_by")

    def perform_destroy(self, instance):
        before = ProjectDocumentSerializer(instance).data
        tenant_id = instance.tenant_id
        document_id = instance.id
        instance.delete()
        write_audit_log(
            tenant_id=tenant_id,
            actor=self.request.user,
            action=PROJECT_MANAGE,
            entity_type="project_document",
            entity_id=document_id,
            before=before,
            after=None,
            ip=client_ip(self.request),
        )
