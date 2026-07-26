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
from io import BytesIO
from typing import Any

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from apps.assets.models import Asset
from apps.assets.services import PHOTO_CONTENT_TYPES, save_attachment_file

from .models import Expense, ExpenseAttachment, Project, ProjectDocument
from .report import (
    AssetRow,
    CategorySpendRow,
    DocumentFile,
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


_INVOICE_SCAN_DPI = 150  # thumbnail-grade, not the huge fitz default -- keeps
# the pre-downscale rasterized PNG a reasonable starting size.

# Code-review finding: the invoice scan now renders LARGE in the report
# (`.invoice-scan` CSS, up to 6in wide, vs. the tiny `.asset-photo` square),
# so it needs a real resolution cap -- otherwise a full-resolution phone
# photo (up to the 25MB per-attachment upload limit) gets base64-inlined
# as-is, bloating the PDF and worker memory. 1600px on the longest edge is
# comfortably print-quality for a ~6in-wide report image (1600px / 6in =
# ~267 DPI) while capping worst-case byte size.
_MAX_INVOICE_SCAN_DIMENSION_PX = 1600

# HEIC/HEIF (`PHOTO_CONTENT_TYPES` includes these -- the iPhone camera
# default, a very real "scanned receipt" case): stock Pillow/WeasyPrint
# cannot decode HEIC at all, so embedding the raw bytes renders a blank/
# broken image with no error. `pillow-heif` has a working musllinux wheel
# for this image's Python 3.12 (verified: `pip download --no-deps
# --only-binary=:all: pillow-heif` against a throwaway `python:3.12-alpine`
# container, then a real encode/decode round trip, per CLAUDE.md's Alpine/
# musl wheel gotcha) and registers itself as a normal Pillow codec plugin, so
# `_resize_and_encode_png` below decodes it transparently once registered --
# same "convert to PNG before embedding" treatment the PDF path already
# gets. If the import somehow fails at runtime anyway, `_HEIF_AVAILABLE`
# gates a graceful `None` (filename-only) fallback specifically for HEIC/
# HEIF rather than embedding something that won't render.
_HEIC_CONTENT_TYPES = frozenset({"image/heic", "image/heif"})
try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    _HEIF_AVAILABLE = True
except Exception:  # noqa: BLE001 - defensive; see docstring above
    _HEIF_AVAILABLE = False


def _resize_and_encode_png(raw_image_bytes: bytes) -> bytes | None:
    """Decode `raw_image_bytes` (any Pillow-openable format, including HEIC/
    HEIF once `pillow_heif.register_heif_opener()` has run), downscale so
    neither dimension exceeds `_MAX_INVOICE_SCAN_DIMENSION_PX`, and re-encode
    as PNG. Shared by both the direct-image and PDF-rasterization paths in
    `_invoice_scan_data_uri` so the size cap is enforced in exactly one
    place. Returns `None` (never raises) on any decode failure -- the caller
    treats that identically to "no preview available".
    """
    from PIL import Image

    try:
        with Image.open(BytesIO(raw_image_bytes)) as img:
            img.load()
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")
            img.thumbnail(
                (_MAX_INVOICE_SCAN_DIMENSION_PX, _MAX_INVOICE_SCAN_DIMENSION_PX),
                Image.LANCZOS,
            )
            out = BytesIO()
            img.save(out, format="PNG", optimize=True)
            return out.getvalue()
    except Exception:  # noqa: BLE001 - deliberately broad, see docstring
        return None


def _invoice_scan_data_uri(attachment: ExpenseAttachment) -> str | None:
    """Best-effort base64 data URI preview for an invoice/receipt scan
    (`include_invoice_scans=True` on `resolve_project_report_data`), mirroring
    `_asset_photo_data_uri`'s "swallow all failures, return None" posture — a
    report with 50 invoice scans must never 500 because ONE is corrupt/
    missing/an unsupported content-type.

    - `image/*` (the `PHOTO_CONTENT_TYPES` allowlist): read the raw bytes off
      `default_storage`, downscale + re-encode via `_resize_and_encode_png`
      (HEIC/HEIF included, once `_HEIF_AVAILABLE` -- see module-level
      comment; falls back to `None` for HEIC/HEIF if that import ever fails).
    - `application/pdf`: rasterize the FIRST PAGE ONLY to a PNG via PyMuPDF
      (`fitz`) at `_INVOICE_SCAN_DPI`, then run THAT through the same
      `_resize_and_encode_png` cap (a large page at 150 DPI can still exceed
      the target dimension).
    - Anything else (DOCX/XLS/XLSX/TXT -- the full `DOC_CONTENT_TYPES`
      allowlist `ExpenseViewSet.attachment` accepts): no preview is possible,
      return `None` so the appendix falls back to filename-only, unchanged
      from today's behavior.
    """
    import base64

    content_type = attachment.content_type or ""
    try:
        if content_type in PHOTO_CONTENT_TYPES:
            if content_type in _HEIC_CONTENT_TYPES and not _HEIF_AVAILABLE:
                return None
            with default_storage.open(attachment.storage_key, "rb") as fh:
                raw = fh.read()
            if not raw:
                return None
            png_bytes = _resize_and_encode_png(raw)
            if not png_bytes:
                return None
            encoded = base64.b64encode(png_bytes).decode("ascii")
            return f"data:image/png;base64,{encoded}"

        if content_type == "application/pdf":
            import fitz

            with default_storage.open(attachment.storage_key, "rb") as fh:
                raw = fh.read()
            if not raw:
                return None
            doc = fitz.open(stream=raw, filetype="pdf")
            try:
                if doc.page_count == 0:
                    return None
                page = doc.load_page(0)
                pix = page.get_pixmap(dpi=_INVOICE_SCAN_DPI)
                rasterized_png = pix.tobytes("png")
            finally:
                doc.close()
            if not rasterized_png:
                return None
            png_bytes = _resize_and_encode_png(rasterized_png)
            if not png_bytes:
                return None
            encoded = base64.b64encode(png_bytes).decode("ascii")
            return f"data:image/png;base64,{encoded}"
    except Exception:  # noqa: BLE001 - deliberately broad, see docstring
        return None

    return None


# Judgment calls (code-review finding: "resource-exhaustion risk ... given
# this repo's own documented NAS-RAM constraint", CLAUDE.md's DS220+ RAM
# concern): both caps below bound `resolve_project_report_data`'s
# `include_project_documents=True` path, which otherwise holds every
# project document's FULL raw bytes in `document_files` at once (up to the
# 25MB per-attachment upload limit, EACH) before the merge step in
# `apps.projects.report` ever runs. `_MAX_APPENDED_DOCUMENT_COUNT` caps how
# many documents are ever read regardless of size; well beyond what any
# real project accumulates. `_MAX_APPENDED_DOCUMENT_TOTAL_BYTES` (200MB)
# caps the aggregate bytes ever held in `document_files` at once -- checked
# against `ProjectDocument.size` (already-recorded upload-time metadata, no
# storage I/O) BEFORE a document is ever read off `default_storage`, so an
# over-budget document is never even read into memory. This is a stronger
# guarantee than `apps.projects.report._MAX_APPENDED_DOCUMENT_PAGES`, which
# only skips MERGING an oversized document AFTER its bytes are already
# resident -- it doesn't bound memory at all on its own.
_MAX_APPENDED_DOCUMENT_COUNT = 50
_MAX_APPENDED_DOCUMENT_TOTAL_BYTES = 200 * 1024 * 1024


def _project_document_file(document: ProjectDocument) -> DocumentFile | None:
    """Best-effort raw-bytes read for one `ProjectDocument`, used only when
    `include_project_documents=True` -- reads `default_storage` here (inside
    `tenant_context`, same as `_asset_photo_data_uri`/`_invoice_scan_data_uri`
    above) so `apps.projects.report._append_project_documents` never has to
    touch storage/the ORM itself (module docstring: report.py stays pure
    data-in). Swallows any read failure and returns `None` -- one missing/
    corrupt document must never fail the whole report, identical posture to
    every other best-effort helper in this module. The label baked in here
    (kind + filename) is what ends up on the divider page.

    Code-review finding #2 ("image documents embed at full resolution"):
    an image content-type (the `PHOTO_CONTENT_TYPES` allowlist, HEIC/HEIF
    included) is downscaled through the SAME `_resize_and_encode_png`
    helper `_invoice_scan_data_uri` already uses (1600px longest-edge cap)
    before being handed off -- `content_type` is normalized to
    `image/png` to match the re-encoded bytes, which
    `apps.projects.report._append_one_document`'s `PHOTO_CONTENT_TYPES`
    check still recognizes. A 25MB phone photo never gets embedded
    byte-for-byte, and doing the downscale here (rather than in
    `apps.projects.report`) avoids report.py importing this module (which
    already imports report.py) while still capping the SAME bytes that
    would otherwise sit in `document_files`.
    """
    try:
        with default_storage.open(document.storage_key, "rb") as fh:
            raw = fh.read()
    except Exception:  # noqa: BLE001 - deliberately broad, see docstring
        return None
    if not raw:
        return None

    content_type = document.content_type or ""
    if content_type in PHOTO_CONTENT_TYPES:
        if content_type in _HEIC_CONTENT_TYPES and not _HEIF_AVAILABLE:
            return None
        png_bytes = _resize_and_encode_png(raw)
        if not png_bytes:
            return None
        raw = png_bytes
        content_type = "image/png"

    label = f"{document.kind.replace('_', ' ').title()} — {document.filename}"
    return DocumentFile(label=label, content_type=content_type, raw_bytes=raw)


def resolve_project_report_data(
    project: Project,
    *,
    include_invoice_scans: bool = False,
    include_project_documents: bool = False,
) -> ProjectReportData:
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

    `include_invoice_scans` (default `False`, opt-in): when `True`, every
    invoice/receipt `ExpenseAttachment` gets a best-effort rasterized preview
    embedded via `_invoice_scan_data_uri` (images inlined directly, PDFs
    rasterized to a PNG of their first page). Left `False`, no storage reads
    or rasterization happen at all for invoices -- embedding scans bloats the
    PDF and is deliberately not the default; the appendix falls back to
    filename-only rows, identical to the report's behavior before this
    option existed.

    `include_project_documents` (default `False`, opt-in): when `True`,
    every `ProjectDocument`'s raw bytes are read off `default_storage` here
    and attached as `ProjectReportData.document_files` (`DocumentFile`s),
    which `apps.projects.report.render_project_report_pdf` then appends as
    full pages onto the rendered PDF (a fitz merge, not an HTML embed — see
    that function's docstring). Left `False`, `document_files` stays empty
    and no storage reads happen for documents at all — the appendix table
    still lists every document by filename/kind either way (`documents`
    below is unconditional).
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
            scan_data_uri = _invoice_scan_data_uri(attachment) if include_invoice_scans else None
            invoices.append(
                InvoiceRow(
                    expense_label=label,
                    filename=attachment.filename,
                    scan_data_uri=scan_data_uri,
                )
            )

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

    project_documents = list(
        ProjectDocument.objects.filter(project=project).order_by("-created_at")
    )
    documents = [DocumentRow(filename=d.filename, kind=d.kind) for d in project_documents]

    document_files: list[DocumentFile] = []
    if include_project_documents:
        total_bytes = 0
        for d in project_documents:
            if len(document_files) >= _MAX_APPENDED_DOCUMENT_COUNT:
                break
            # `d.size` is already-recorded upload-time metadata -- checked
            # BEFORE touching `default_storage` so a document that would
            # blow the aggregate budget is never even read into memory
            # (code-review finding #1 -- see the caps' own docstring above).
            if total_bytes + d.size > _MAX_APPENDED_DOCUMENT_TOTAL_BYTES:
                break
            document_file = _project_document_file(d)
            if document_file is None:
                continue
            document_files.append(document_file)
            total_bytes += len(document_file.raw_bytes)

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
        document_files=document_files,
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
