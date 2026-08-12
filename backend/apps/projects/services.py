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
from decimal import ROUND_HALF_UP, Decimal
from io import BytesIO
from typing import Any, Protocol

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.utils.text import slugify

from apps.assets.models import Asset
from apps.assets.services import PHOTO_CONTENT_TYPES, save_attachment_file

from .models import Expense, Project, ProjectDocument
from .report import (
    AssetRow,
    CategorySpendRow,
    DocumentFile,
    DocumentRow,
    ExpenseRow,
    InvoiceRow,
    ProjectReportData,
)


def split_amount_evenly(total: Decimal, parts: int) -> list[Decimal]:
    """Split `total` into `parts` 2dp shares that sum EXACTLY back to `total`.

    Largest-remainder allocation: floor every share to the cent, then hand the
    leftover cents out one each to the earliest shares. `Decimal(total) / 3`
    naively rounded gives 3 x 33.33 = 99.99 for a 100.00 line — the missing
    cent is exactly the kind of unexplained residual an auditor asks about, and
    the reason this is a shared helper rather than an inline division. M8 §3
    mandates the same technique for the FX and shipping/tax splits in Phase 2,
    which is why this lives in services and not in the serializer.

    `parts` <= 0 returns []; a negative `total` (a credit) splits the same way,
    with the leftover cents applied in the same direction.
    """
    if parts <= 0:
        return []

    cents_total = int((total * 100).to_integral_value(rounding=ROUND_HALF_UP))
    base, remainder = divmod(abs(cents_total), parts)
    sign = -1 if cents_total < 0 else 1
    shares = [base + (1 if i < remainder else 0) for i in range(parts)]
    return [Decimal(sign * share) / Decimal(100) for share in shares]


def resolve_asset_allocations(expense: Expense) -> dict[int, Decimal]:
    """`{link_id: resolved amount}` for one expense's asset links.

    Links carrying an explicit `allocated_amount` resolve to exactly that.
    Links with `NULL` ("auto", see `sync_expense_asset_links`) share out
    whatever is left of `expense.amount` after the explicit ones, via
    `split_amount_evenly` so the auto part sums to the cent.

    Resolving at read time — rather than freezing a number at write time — is
    what keeps an auto share correct across later edits of the line's total.
    Explicit amounts are never touched, so a figure someone typed off a receipt
    survives verbatim.

    Reads `expense.asset_links` through the prefetch when one is primed
    (`asset_links__asset` on both expense querysets), so this adds no query on
    the list path.
    """
    links = list(expense.asset_links.all())
    explicit_total = sum(
        (link.allocated_amount for link in links if link.allocated_amount is not None),
        Decimal("0"),
    )
    auto_links = [link for link in links if link.allocated_amount is None]
    remainder = (expense.amount or Decimal("0")) - explicit_total
    auto_shares = split_amount_evenly(remainder, len(auto_links))

    resolved = {
        link.id: link.allocated_amount for link in links if link.allocated_amount is not None
    }
    for link, share in zip(auto_links, auto_shares, strict=True):
        resolved[link.id] = share
    return resolved


def sync_expense_asset_links(
    expense: Expense, allocations: list[tuple[Asset, Decimal | None, int]]
) -> None:
    """Make `expense`'s `ExpenseAssetLink` rows exactly match `allocations`.

    Each entry is `(asset, allocated_amount_or_None, quantity)`. Set semantics:
    links to assets no longer listed are deleted, listed ones created/updated.

    **An even split is a FALLBACK, never the rule.** A real receipt does not
    divide equally — a $350 GPU and a $9 cable on one line are not $179.50
    each — so a caller that knows the per-item prices passes them and they are
    stored verbatim.

    **`allocated_amount = NULL` means "auto"**, and is stored as NULL rather
    than materialized into a number. That distinction is load-bearing: it is
    the only way to tell a figure a human typed from one the system guessed,
    and without it, editing the line's total either silently overwrites the
    user's numbers or silently leaves them stale. `resolve_asset_allocations`
    below turns NULL into a concrete share at read time — an even split of
    whatever is left after the explicit amounts are subtracted — so an auto
    share can never drift from the line total, no matter how often the total
    is edited.

    Deliberately does NOT force the links to sum to `expense.amount`: the user
    may be mid-edit, and blocking a save on arithmetic is the wrong trade
    (M8's "warn, never block" rule). The UI shows the running variance;
    reconciliation reporting in Phase 2 is what surfaces a mismatch.

    **`Expense.asset` is kept in sync with the first link** for as long as that
    deprecated column exists (M8 §8): every M7-era reader — the CSV export, the
    report's asset section, `attachment_from_asset` — still goes through it, so
    letting it fall stale would silently corrupt those outputs during the one
    release the column is still around. Clearing the list clears the FK.

    Callers must already hold the request transaction (`ATOMIC_REQUESTS`), and
    must have scoped every `asset` through a tenant-scoped queryset first —
    this function trusts what it is handed (R4: see
    `ExpenseSerializer.get_fields`, which restricts the selectable assets to
    the expense's own project plus unassigned ones).
    """
    from .models import ExpenseAssetLink

    wanted_ids = [asset.id for asset, _amount, _qty in allocations]
    ExpenseAssetLink.objects.filter(expense=expense).exclude(asset_id__in=wanted_ids).delete()

    existing = {link.asset_id: link for link in ExpenseAssetLink.objects.filter(expense=expense)}

    for asset, amount, quantity in allocations:
        # An UNASSIGNED asset (general pool) being expensed to this project is
        # this project's fund paying for it — so its funding project follows the
        # money. Without this the asset stays invisible on the project's Assets
        # page while its cost sits in that project's ledger, which is exactly
        # the "who paid for this" ambiguity M8 exists to remove. An asset
        # already funded by ANOTHER project is never reassigned here: the
        # serializer's queryset makes it unselectable in the first place.
        if asset.project_id is None:
            asset.project_id = expense.project_id
            asset.save(update_fields=["project"])

        link = existing.get(asset.id)
        if link is None:
            ExpenseAssetLink.objects.create(
                tenant_id=expense.tenant_id,
                expense=expense,
                asset=asset,
                allocated_amount=amount,
                quantity=quantity,
            )
        elif link.allocated_amount != amount or link.quantity != quantity:
            link.allocated_amount = amount
            link.quantity = quantity
            link.save(update_fields=["allocated_amount", "quantity"])

    new_primary_id = wanted_ids[0] if wanted_ids else None
    if expense.asset_id != new_primary_id:
        expense.asset_id = new_primary_id
        expense.save(update_fields=["asset"])


def _overhead_total(project: Project) -> Decimal:
    """Total shipping + tax across every receipt booked to `project`.

    Imported lazily for the same import-loop reason as `_reconciliation_rows`
    (finance already imports projects). Returns zero for a project with no
    orders, so pre-M8 projects are unaffected.
    """
    from django.db.models import DecimalField, Sum, Value
    from django.db.models.functions import Coalesce

    from apps.finance.models import Purchase

    zero = Value(Decimal("0.00"), output_field=DecimalField(max_digits=14, decimal_places=2))
    totals = Purchase.objects.filter(payment__project=project).aggregate(
        shipping=Coalesce(Sum("shipping"), zero),
        tax=Coalesce(Sum("tax"), zero),
    )
    return (totals["shipping"] or Decimal("0.00")) + (totals["tax"] or Decimal("0.00"))


def budget_rollup(project: Project) -> dict[str, Any]:
    """Compute the M7 budget rollup for `project` in two aggregated queries
    (`docs/tasks/M7-project-grants.md`: "Compute with a single aggregated
    query (no N+1)" — still no N+1; M8 added a second fixed-cost aggregate for
    receipt overhead, see `_overhead_total`):

    - `budget_total`: `project.budget_total` (or `None` if not set).
    - `spent`: every `Expense.amount` booked against this project, **plus** the
      shipping and tax on its receipts — i.e. what actually left the account,
      not just what the line items list.
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

    # Shipping and tax are held on the receipt (`Purchase`), not on the item
    # lines, so summing `Expense.amount` alone understates what the project
    # actually spent by exactly the overhead — the project would show less
    # spent than the bank statement proves was taken, which is the one
    # discrepancy an audit cannot survive. Because an order belongs to exactly
    # one project, ALL of its overhead is this project's; there is no share to
    # apportion elsewhere. Costs one extra aggregate query, which keeps the
    # figures derived rather than denormalized onto `Expense`.
    overhead = _overhead_total(project)
    spent += overhead

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
    if overhead:
        # Its own row rather than silently folded into a category: overhead is
        # real spend but it is not equipment, and an auditor reading a category
        # breakdown should see why the categories do not sum to the total.
        spend_by_category.append(
            {"category_id": None, "category": "Shipping & tax", "total": overhead}
        )

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
        with Image.open(BytesIO(raw_image_bytes)) as opened:
            opened.load()
            # `convert()` returns a plain Image, not the ImageFile `open()`
            # gives back — bind it to its own name so the two stay distinct.
            img = opened.convert("RGB") if opened.mode not in ("RGB", "RGBA") else opened
            img.thumbnail(
                (_MAX_INVOICE_SCAN_DIMENSION_PX, _MAX_INVOICE_SCAN_DIMENSION_PX),
                Image.Resampling.LANCZOS,
            )
            out = BytesIO()
            img.save(out, format="PNG", optimize=True)
            return out.getvalue()
    except Exception:  # noqa: BLE001 - deliberately broad, see docstring
        return None


def _scan_document_file(label: str, attachment: _ScannableAttachment) -> DocumentFile | None:
    """Raw bytes for a PDF receipt/statement scan, so its FULL pages can be
    appended to the report by `apps.projects.report.render_project_report_pdf`.

    Only PDFs come through here: an image scan is a single page and still
    embeds inline as a preview, so nothing about it can be truncated. Same
    best-effort posture as every other storage read in this module — a
    corrupt or missing scan yields `None` and costs that one document, never
    the report.
    """
    if (attachment.content_type or "") != "application/pdf":
        return None
    try:
        with default_storage.open(attachment.storage_key, "rb") as fh:
            raw = fh.read()
    except Exception:  # noqa: BLE001 - deliberately broad, see docstring
        return None
    if not raw:
        return None
    return DocumentFile(label=label, content_type="application/pdf", raw_bytes=raw)


class _ScannableAttachment(Protocol):
    """The only surface `_invoice_scan_data_uri` needs off an attachment.

    Structural rather than a concrete model because three unrelated tables —
    `ExpenseAttachment` (pre-M8), `PaymentAttachment` (bank statements) and
    `PurchaseAttachment` (receipt scans) — all get rasterized by that one
    helper. Nothing about reading bytes off storage cares which table they
    came from.
    """

    content_type: str
    storage_key: str


def _invoice_scan_data_uri(attachment: _ScannableAttachment) -> str | None:
    """Best-effort base64 data URI preview for an invoice/receipt scan
    (`include_invoice_scans=True` on `resolve_project_report_data`), mirroring
    `_asset_photo_data_uri`'s "swallow all failures, return None" posture — a
    report with 50 invoice scans must never 500 because ONE is corrupt/
    missing/an unsupported content-type.

    - `image/*` (the `PHOTO_CONTENT_TYPES` allowlist): read the raw bytes off
      `default_storage`, downscale + re-encode via `_resize_and_encode_png`
      (HEIC/HEIF included, once `_HEIF_AVAILABLE` -- see module-level
      comment; falls back to `None` for HEIC/HEIF if that import ever fails).
    - `application/pdf`: **no inline preview at all** — returns `None`, and
      `_scan_document_file` supplies the raw bytes so every page is appended
      to the report in full. This used to rasterize the first page only,
      which meant a 6-page receipt appeared in the report as a single page
      with nothing indicating the rest existed.
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

        # A PDF scan is deliberately NOT rasterized to an inline preview here.
        # It used to be — first page only — which quietly truncated a 6-page
        # receipt to one page in a document whose whole purpose is completeness.
        # PDFs now go through `_scan_document_file` instead and are appended in
        # full, at original quality, by `render_project_report_pdf`.
        if content_type == "application/pdf":
            return None
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

    # ONE materialized pass over the orders, shared by everything downstream:
    # the order numbering, each order's scans and the overhead spread all
    # derive from this same list, so they cannot disagree with each other.
    orders = list(_report_orders(project))

    # `Payment` -> "Order N", in statement order. `Purchase` -> its payment is
    # what lets a pre-M8 expense attachment find the order it belongs to, so
    # it renders with that order rather than orphaned at the end of the report.
    charge_number: dict[int, int] = {o.id: n for n, o in enumerate(orders, start=1)}
    order_of_purchase: dict[int, int] = {
        purchase.id: order.id for order in orders for purchase in order.purchases.all()
    }

    # One row per EXPENSE — i.e. per order — not per item. See `ExpenseRow`.
    expense_rows: list[ExpenseRow] = []
    for order in orders:
        items = [item for purchase in order.purchases.all() for item in purchase.expenses.all()]
        item_cost = sum((item.amount or Decimal("0.00") for item in items), Decimal("0.00"))
        overhead = sum(
            (
                (purchase.shipping or Decimal("0.00")) + (purchase.tax or Decimal("0.00"))
                for purchase in order.purchases.all()
            ),
            Decimal("0.00"),
        )
        expense_rows.append(
            ExpenseRow(
                seq=charge_number[order.id],
                date=order.paid_on.isoformat() if order.paid_on else "",
                vendor=order.vendor or "Unknown vendor",
                item_count=len(items),
                amount=item_cost,
                overhead=overhead,
                loaded=item_cost + overhead,
                charged=order.amount or Decimal("0.00"),
            )
        )

    # Pre-M8 expenses that were never linked to a receipt stand alone: they are
    # real spending and must appear, or the table would not sum to the project's
    # total. `seq=0` renders as a dash — there is no order to point at.
    for e in expenses:
        if e.purchase_id and e.purchase_id in order_of_purchase:
            continue
        expense_rows.append(
            ExpenseRow(
                seq=0,
                date=e.date.isoformat(),
                vendor=e.vendor or "Unknown vendor",
                item_count=1,
                amount=e.amount,
                overhead=Decimal("0.00"),
                loaded=e.amount,
                charged=e.amount,
            )
        )

    expense_rows.sort(key=lambda row: (row.date, row.seq))

    invoices: list[InvoiceRow] = []
    orphan_invoices: list[InvoiceRow] = []
    # Scans are grouped BY ORDER rather than pooled, so each order's pages can
    # be merged directly beneath it (PDFs) or embedded in its block (images)
    # instead of forming one undifferentiated pile at the end of the report.
    scans_by_charge: dict[int, list[DocumentFile]] = {}
    images_by_charge: dict[int, list[InvoiceRow]] = {}
    legacy_scan_files: list[DocumentFile] = []
    scan_bytes_budget = _MAX_APPENDED_DOCUMENT_TOTAL_BYTES

    def _invoice_row(
        label: str,
        attachment,
        bucket: list[DocumentFile],
        image_bucket: list[InvoiceRow] | None,
    ) -> InvoiceRow:
        nonlocal scan_bytes_budget
        row = InvoiceRow(
            expense_label=label,
            filename=attachment.filename,
            scan_data_uri=_invoice_scan_data_uri(attachment) if include_invoice_scans else None,
        )
        if include_invoice_scans:
            # PDFs are appended in full as pages; images embed inline in the
            # order's block. Exactly one of the two happens per attachment, and
            # both land with the same order — the file format decides HOW a
            # scan is shown, never WHERE.
            scan = _scan_document_file(f"{label} — {attachment.filename}", attachment)
            if scan is not None:
                # Same aggregate byte ceiling that bounds project documents:
                # every appended scan's raw bytes are resident at once, and
                # this NAS has little RAM to spare. An over-budget scan still
                # gets its listing — it just isn't appended in full.
                size = len(scan.raw_bytes)
                if size <= scan_bytes_budget:
                    scan_bytes_budget -= size
                    bucket.append(scan)
            elif row.scan_data_uri and image_bucket is not None:
                image_bucket.append(row)
        return row

    # Order paperwork (M8): the bank statement hangs off the `Payment` and each
    # receipt scan off its `Purchase`. This is where essentially all real
    # paperwork now lives — the loop below it only covers pre-M8 expenses that
    # still carry their own `ExpenseAttachment`. Omitting this section is the
    # bug that made an order's receipts absent from the report entirely (not
    # even a filename row) while the reconciliation table above it correctly
    # reported the same scan as "on file" — the two sections disagreeing about
    # the same document is precisely what an audit cannot afford.
    for order in orders:
        bucket = scans_by_charge.setdefault(order.id, [])
        images = images_by_charge.setdefault(order.id, [])
        number = charge_number[order.id]
        paid_on = order.paid_on.isoformat() if order.paid_on else "undated"
        order_label = f"Order {number} — {paid_on} · {order.vendor or 'Unknown vendor'}"
        for attachment in order.attachments.all():
            invoices.append(
                _invoice_row(f"{order_label} (bank statement)", attachment, bucket, images)
            )

        purchases = list(order.purchases.all())
        for index, purchase in enumerate(purchases, start=1):
            where = f"receipt {index} of {len(purchases)}" if len(purchases) > 1 else "receipt"
            if purchase.receipt_number:
                where = f"{where} #{purchase.receipt_number}"
            for attachment in purchase.attachments.all():
                invoices.append(
                    _invoice_row(f"{order_label} ({where})", attachment, bucket, images)
                )

    # Pre-M8 expenses that own their invoice scan directly. If the expense has
    # since been linked to a receipt, the scan belongs to that receipt's order
    # and renders there like any other — where the paperwork is *stored* is a
    # schema detail and must not decide where it appears in the document. Only
    # a scan whose expense was never linked to a receipt has no order to sit
    # under; those are the sole remaining entries in the appendix.
    for e in expenses:
        order_id = order_of_purchase.get(e.purchase_id) if e.purchase_id else None
        if order_id is not None:
            label = f"Order {charge_number[order_id]} — {e.vendor or 'Unknown vendor'}"
            bucket = scans_by_charge.setdefault(order_id, [])
            images = images_by_charge.setdefault(order_id, [])
        else:
            label = f"{e.date.isoformat()} - {e.vendor or 'Unknown vendor'} - {e.amount}"
            bucket = legacy_scan_files
            images = None  # type: ignore[assignment]

        for attachment in e.attachments.all():
            row = _invoice_row(label, attachment, bucket, images)
            invoices.append(row)
            if order_id is None:
                orphan_invoices.append(row)

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
        reconciliation=_reconciliation_rows(
            orders, charge_number, scans_by_charge, images_by_charge
        ),
        assets=asset_rows,
        documents=documents,
        invoices=invoices,
        orphan_invoices=orphan_invoices,
        document_files=document_files,
        scan_files=legacy_scan_files,
    )


def _report_orders(project):
    """The project's orders with all paperwork prefetched, in statement order.

    Deliberately shared by BOTH report sections that read orders — the
    reconciliation table (`_reconciliation_rows`) and the appendix's scan list
    (`resolve_project_report_data`). They previously read different tables,
    which let the reconciliation table say a receipt was "on file" while the
    appendix omitted it. One queryset makes that class of disagreement
    unrepresentable rather than merely fixed once.
    """
    return (
        project.orders.prefetch_related(
            "attachments", "purchases__attachments", "purchases__expenses"
        )
        .select_related("tenant")
        .order_by("paid_on", "id")
    )


def _reconciliation_rows(orders, charge_number, scans_by_charge, images_by_charge) -> list:
    """Build the report's charges section (M8 §6.2) from already-materialized
    `orders`.

    One row per bank charge, its shipments, whether the paperwork is on file,
    and **the charge's own scan bytes** so the renderer can place them directly
    beneath it. Because an order belongs to exactly one project there is no
    cross-project remainder to redact — the whole charge is this project's.

    Takes the orders list rather than the project so the numbering here is the
    identical numbering the ledger's `charge_ref` column points at; deriving it
    twice from two queries is exactly how the two would drift apart.
    """
    # `_money` and `resolve_payment` both live in the finance app; imported
    # lazily so `apps.projects` takes no hard import dependency on it at
    # module load (finance already imports projects — a top-level import
    # here would close that loop).
    from apps.finance.services import _money, resolve_payment

    from .report import ReconciliationOrderRow, ReconciliationShipmentRow

    rows = []
    for order in orders:
        resolution = resolve_payment(order)
        by_purchase = {r.purchase_id: r for r in resolution.purchases}
        purchases = list(order.purchases.all())

        shipments = []
        for index, purchase in enumerate(purchases, start=1):
            detail = by_purchase.get(purchase.id)
            settled = detail.settled_amount if detail else None
            settlement_currency = detail.settlement_currency if detail else None
            converted = (
                _money(settled, settlement_currency or "")
                if settled is not None
                and settlement_currency
                and settlement_currency != purchase.currency
                else ""
            )
            shipments.append(
                ReconciliationShipmentRow(
                    label=(
                        f"Shipment {index} of {len(purchases)}" if len(purchases) > 1 else "Items"
                    ),
                    receipt_number=purchase.receipt_number,
                    total=_money(purchase.total, purchase.currency),
                    converted=converted,
                    item_count=len(purchase.expenses.all()),
                    has_receipt_scan=bool(purchase.attachments.all()),
                    items_subtotal=_money(
                        sum(
                            (e.amount or Decimal("0.00") for e in purchase.expenses.all()),
                            Decimal("0.00"),
                        ),
                        purchase.currency,
                    ),
                    shipping=_money(purchase.shipping or Decimal("0.00"), purchase.currency),
                    tax=_money(purchase.tax or Decimal("0.00"), purchase.currency),
                )
            )

        rows.append(
            ReconciliationOrderRow(
                paid_on=order.paid_on.isoformat() if order.paid_on else "",
                vendor=order.vendor,
                amount=_money(order.amount, order.currency),
                account_label=order.account_label,
                statement_ref=order.statement_ref,
                items_total=_money(resolution.allocated, order.currency),
                variance_note=(
                    "fully itemized"
                    if resolution.is_balanced
                    else _money(resolution.variance, order.currency) + " unaccounted for"
                ),
                is_balanced=resolution.is_balanced,
                has_statement_scan=bool(order.attachments.all()),
                shipments=shipments,
                seq=charge_number[order.id],
                scan_files=scans_by_charge.get(order.id, []),
                image_scans=images_by_charge.get(order.id, []),
            )
        )
    return rows


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


def project_archive_storage_key(tenant_id: int, job_id) -> str:
    """Same tenant + job-scoped layout as `project_report_storage_key` — the
    job's own unguessable UUID is the collision-proofing."""
    return f"project-archives/{tenant_id}/{job_id}.zip"


def save_project_archive(*, tenant_id: int, job_id, project, fileobj) -> tuple[str, str]:
    """Persist a built ZIP bundle (`apps.projects.archive.build_project_archive`)
    to the SAME storage backend/volume every other attachment/report lands on.

    Takes a FILE OBJECT, not bytes, on purpose: the archive is streamed into a
    `SpooledTemporaryFile` precisely so a large bundle never has to exist in
    RAM as one `bytes` — reading it back with `.read()` just to hand
    `ContentFile` a buffer would throw that away. `django.core.files.File`
    wraps it so `default_storage.save` streams it out in chunks.
    """
    from django.core.files import File

    key = project_archive_storage_key(tenant_id, job_id)
    storage_key = default_storage.save(key, File(fileobj))
    slug = slugify(project.code or project.name) or f"project-{project.id}"
    filename = f"{slug}-archive-{uuid.UUID(str(job_id)).hex[:8]}.zip"
    return storage_key, filename
