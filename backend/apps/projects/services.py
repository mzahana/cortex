"""Business rules kept out of serializers/views for the M7 project hub
(`docs/tasks/M7-project-grants.md`), per CLAUDE.md: "keep serializers thin;
put business rules in services/model methods".

1. `budget_rollup` — the single-aggregated-query budget-vs-spend computation
   (`budget_total`, `spent`, `remaining`, `spend_by_category`) shared by the
   project detail serializer and the report PDF (Slice 3, below).
2. `save_project_document_file` / `save_expense_attachment_file` — thin
   wrappers around `apps.assets.services.save_attachment_file` (the ONLY
   writer of attachment bytes in this codebase), each with a distinct storage
   `prefix` so project documents/expense attachments/asset attachments never
   share a directory even if an id coincidentally collides across models.
3. `resolve_project_report_data` / `save_project_report_pdf` (Slice 3,
   `pwa-scan-specialist`) — the ONLY place that turns a `Project`'s DB state
   into the plain `apps.projects.report.ProjectReportData` the WeasyPrint
   renderer needs, and the ONLY writer of the rendered PDF's bytes, mirroring
   `apps.labels.services.resolve_label_data`/`save_label_pdf` exactly.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from apps.assets.models import Asset
from apps.assets.services import save_attachment_file

from .models import Expense, Project, ProjectDocument
from .report import (
    AssetRow,
    CategorySpendRow,
    DocumentRow,
    ExpenseRow,
    InvoiceRow,
    ProjectReportData,
)


def budget_rollup(project: Project) -> dict[str, Any]:
    """Compute the M7 budget rollup for `project` in exactly ONE query
    (`docs/tasks/M7-project-grants.md`: "Compute with a single aggregated
    query (no N+1)"):

    - `budget_total`: `project.budget_total` (or `None` if not set).
    - `spent`: sum of every `Expense.amount` booked against this project.
    - `remaining`: `budget_total - spent` (`None` if no budget was set — a
      project with no awarded budget has nothing to be "remaining" against).
    - `spend_by_category`: `[{"category_id", "category", "total"}, ...]`,
      ordered by category name; an expense with no category (the FK is
      `SET_NULL`) is grouped under `category_id=None`/`category="Uncategorized"`.

    One `.values(...).annotate(Sum(...))` query returns every category's
    total; summing those totals in Python (rather than a second `.aggregate()`
    call) is what keeps this to a single round-trip.
    """
    from django.db.models import Sum

    category_totals = list(
        Expense.objects.filter(project=project)
        .values("category_id", "category__name")
        .annotate(total=Sum("amount"))
        .order_by("category__name")
    )

    spent = sum(
        (row["total"] for row in category_totals if row["total"] is not None), Decimal("0.00")
    )
    budget_total = project.budget_total
    remaining = (budget_total - spent) if budget_total is not None else None

    spend_by_category = [
        {
            "category_id": row["category_id"],
            "category": row["category__name"] or "Uncategorized",
            "total": row["total"] or Decimal("0.00"),
        }
        for row in category_totals
    ]

    return {
        "budget_total": budget_total,
        "spent": spent,
        "remaining": remaining,
        "spend_by_category": spend_by_category,
    }


def save_project_document_file(
    *, tenant_id: int, project_id: int, uploaded_file
) -> tuple[str, str, int]:
    """`ProjectDocument` upload — see module docstring."""
    return save_attachment_file(
        tenant_id=tenant_id,
        anchor_id=project_id,
        uploaded_file=uploaded_file,
        prefix="project-documents",
    )


def save_expense_attachment_file(
    *, tenant_id: int, expense_id: int, uploaded_file
) -> tuple[str, str, int]:
    """`ExpenseAttachment` (invoice scan) upload — see module docstring."""
    return save_attachment_file(
        tenant_id=tenant_id,
        anchor_id=expense_id,
        uploaded_file=uploaded_file,
        prefix="expense-attachments",
    )


# --- Report PDF (Slice 3, `apps.projects.tasks.generate_project_report_pdf`) -


def _asset_photo_data_uri(asset: Asset) -> str | None:
    """Best-effort base64 data URI for `asset`'s most recently uploaded
    photo attachment (task spec: "include a thumbnail ... if one exists and
    it renders cleanly (skip gracefully if not)"). `asset.attachments` is
    prefetched by the caller ordered `-created_at` (`Attachment.Meta.
    ordering`), so `[0]` is the most recent — "primary" here just means
    "latest photo on file", there is no separate primary-photo flag on
    `Attachment`. Any failure reading the file off the storage backend (
    missing/corrupt, non-image content type, ...) is swallowed and `None` is
    returned rather than failing the whole report render — a report with 50
    assets should never 500 because ONE photo went missing from the volume.
    """
    import base64

    photo = next((a for a in asset.attachments.all() if a.kind == "photo"), None)
    if photo is None:
        return None
    content_type = photo.content_type or ""
    if not content_type.startswith("image/"):
        return None
    try:
        with default_storage.open(photo.storage_key, "rb") as fh:
            raw = fh.read()
    except Exception:  # noqa: BLE001 - deliberately broad, see docstring
        return None
    if not raw:
        return None
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{content_type};base64,{encoded}"


def resolve_project_report_data(project: Project) -> ProjectReportData:
    """Build the `ProjectReportData` the WeasyPrint renderer
    (`apps.projects.report`) needs for `project`'s audit report — the ONLY
    place DB rows are turned into report display data (mirrors
    `apps.labels.services.resolve_label_data`'s role for label PDFs). Must be
    called from inside `tenant_context(...)` (the Celery task's job) so every
    query below runs RLS-scoped as the app role, same as every other
    tenant-owned read in this codebase.

    Deliberately does NOT reuse `apps.assets.api.visible_assets_queryset`
    for the asset inventory table: that helper (a) needs a live `request`
    (unavailable inside a Celery task) to compute its RBAC row-scope, and
    (b) excludes retired assets by default — the wrong default for an audit/
    grant-closure report, which should show every asset ever purchased under
    the project (including retired/lost) with its actual status. The report
    endpoint (`apps.projects.api.ProjectViewSet.report`) already gates entry
    on `expense.view` scoped to THIS project before a job is ever created, so
    a plain tenant-scoped `Asset.objects.filter(project_id=...)` here carries
    the same effective row-scope for the one caller who can reach this task.
    """
    rollup = budget_rollup(project)

    spend_by_category = [
        CategorySpendRow(category=row["category"], total=row["total"])
        for row in rollup["spend_by_category"]
    ]

    expenses_qs = (
        Expense.objects.filter(project=project)
        .select_related("category")
        .prefetch_related("attachments")
        .order_by("date", "id")
    )
    expenses = list(expenses_qs)
    expense_rows = [
        ExpenseRow(
            date=e.date.isoformat(),
            category=e.category.name if e.category else "Uncategorized",
            vendor=e.vendor,
            invoice_number=e.invoice_number,
            description=e.description,
            amount=e.amount,
        )
        for e in expenses
    ]

    invoices: list[InvoiceRow] = []
    for e in expenses:
        label = f"{e.date.isoformat()} - {e.vendor or 'Unknown vendor'} - {e.amount}"
        for attachment in e.attachments.all():
            invoices.append(InvoiceRow(expense_label=label, filename=attachment.filename))

    assets_qs = (
        Asset.objects.filter(project_id=project.id)
        .select_related("category")
        .prefetch_related("attachments")
        .order_by("name")
    )
    asset_rows = [
        AssetRow(
            name=a.name,
            category=a.category.name if a.category else "",
            serial_number=a.serial_number,
            purchase_cost=a.purchase_cost,
            status=a.status,
            photo_data_uri=_asset_photo_data_uri(a),
        )
        for a in assets_qs
    ]

    documents = [
        DocumentRow(filename=d.filename, kind=d.kind)
        for d in ProjectDocument.objects.filter(project=project).order_by("-created_at")
    ]

    return ProjectReportData(
        tenant_name=project.tenant.name,
        name=project.name,
        code=project.code,
        funding_source=project.funding_source,
        sponsor=project.sponsor,
        lead_name=project.lead_user.name if project.lead_user else "",
        start_date=project.start_date.isoformat() if project.start_date else None,
        end_date=project.end_date.isoformat() if project.end_date else None,
        status=project.status,
        currency=project.currency,
        budget_total=rollup["budget_total"],
        spent=rollup["spent"],
        remaining=rollup["remaining"],
        spend_by_category=spend_by_category,
        expenses=expense_rows,
        assets=asset_rows,
        documents=documents,
        invoices=invoices,
    )


def project_report_storage_key(tenant_id: int, job_id) -> str:
    """Storage key layout: tenant + job-scoped, same reasoning as
    `apps.labels.services.label_pdf_storage_key` — the job's own unguessable
    UUID is the collision-proofing, no separate random suffix needed.
    """
    return f"project-reports/{tenant_id}/{job_id}.pdf"


def save_project_report_pdf(*, tenant_id: int, job_id, pdf_bytes: bytes) -> tuple[str, str]:
    """Write the rendered report PDF to the SAME storage backend/volume every
    other attachment/label PDF uses; returns `(storage_key, filename)` — the
    only things ever persisted on `Job` (the bytes never enter the DB, same
    rule as `apps.labels.services.save_label_pdf`).
    """
    key = project_report_storage_key(tenant_id, job_id)
    storage_key = default_storage.save(key, ContentFile(pdf_bytes))
    filename = f"project-report-{uuid.UUID(str(job_id)).hex[:8]}.pdf"
    return storage_key, filename
