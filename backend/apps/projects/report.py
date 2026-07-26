"""Project audit report PDF rendering (M7 Slice 3,
`docs/tasks/M7-project-grants.md` "Build" section: "A **project audit report
PDF** rendered by WeasyPrint in a Celery task").

Mirrors `apps.labels.rendering` exactly, same shape/reasoning: a handful of
frozen, plain dataclasses hold everything the render needs (never leaking an
`Asset`/`Expense`/`Project` ORM instance into this module — `apps.projects.
services.resolve_project_report_data` is the only thing that builds a
`ProjectReportData` from the DB, staying entirely inside `tenant_context`);
`render_project_report_pdf` turns that into an HTML document and rasterizes
it with WeasyPrint. `apps.projects.tasks.generate_project_report_pdf` is the
only caller of the render entrypoint.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from decimal import Decimal
from io import BytesIO

from weasyprint import HTML

from apps.assets.services import PHOTO_CONTENT_TYPES


@dataclass(frozen=True)
class CategorySpendRow:
    category: str
    total: Decimal


@dataclass(frozen=True)
class ExpenseRow:
    date: str  # ISO 8601 (`date.isoformat()`), already resolved server-side
    category: str
    vendor: str
    invoice_number: str
    description: str
    amount: Decimal


@dataclass(frozen=True)
class AssetRow:
    name: str
    category: str
    serial_number: str
    purchase_cost: Decimal | None
    status: str
    photo_data_uri: str | None = None


@dataclass(frozen=True)
class DocumentRow:
    filename: str
    kind: str


@dataclass(frozen=True)
class DocumentFile:
    """Raw bytes for one `ProjectDocument`, resolved inside `tenant_context`
    by `apps.projects.services.resolve_project_report_data` (the only thing
    in this codebase allowed to touch `default_storage`/the ORM for this
    purpose — see this module's docstring) and consumed ONLY by
    `_append_project_documents` below to append full pages onto the
    rendered report. Never touched by the WeasyPrint HTML path — this is a
    fitz/PDF-merge concern, not an HTML-embedding one, unlike
    `InvoiceRow.scan_data_uri`.
    """

    label: str  # e.g. "Progress report - <filename>", used on the divider page
    content_type: str
    raw_bytes: bytes


@dataclass(frozen=True)
class InvoiceRow:
    expense_label: str  # e.g. "2026-01-15 - Acme Corp - $1,200.00"
    filename: str
    scan_data_uri: str | None = None


@dataclass(frozen=True)
class ProjectReportData:
    """Everything one report needs, already tenant/RBAC-resolved by
    `apps.projects.services.resolve_project_report_data` — see that
    function's docstring for exactly how each field is computed.
    """

    # --- header ---------------------------------------------------------
    tenant_name: str
    name: str
    code: str
    funding_source: str
    sponsor: str
    lead_name: str
    start_date: str | None
    end_date: str | None
    status: str

    # --- budget summary ---------------------------------------------------
    currency: str
    budget_total: Decimal | None
    spent: Decimal
    remaining: Decimal | None
    spend_by_category: list[CategorySpendRow] = field(default_factory=list)

    # --- itemized ledger / inventory / appendix --------------------------
    expenses: list[ExpenseRow] = field(default_factory=list)
    assets: list[AssetRow] = field(default_factory=list)
    documents: list[DocumentRow] = field(default_factory=list)
    invoices: list[InvoiceRow] = field(default_factory=list)

    # Only populated when `include_project_documents=True` was passed to
    # `resolve_project_report_data` — the raw bytes backing the full-page
    # appends `_append_project_documents` does after the WeasyPrint render.
    # Empty list (not None) when the flag is off, mirroring every other
    # list field's "empty means nothing to render" convention here.
    document_files: list[DocumentFile] = field(default_factory=list)


def _money(amount: Decimal | None, currency: str) -> str:
    """Currency-aware formatting (task spec: "Currency-aware formatting;
    handle the no-budget-set ... case without crashing"). `currency` is a
    free-text 3-letter code (`Project.currency`/`Expense.currency`, never
    validated against ISO-4217 elsewhere in this codebase) prefixed onto the
    amount rather than resolved to a locale-aware symbol — same "plain code,
    not a symbol table" posture the rest of the app takes with currency.
    """
    if amount is None:
        return "Not set"
    formatted = f"{amount:,.2f}"
    return f"{currency} {formatted}" if currency else formatted


def _esc(value: str | None) -> str:
    return html.escape(value or "")


_REPORT_CSS = """
@page {
  size: letter portrait;
  margin: 0.6in 0.5in;
  @bottom-center {
    content: "Page " counter(page) " of " counter(pages);
    font-size: 8pt;
    color: #666;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: Helvetica, Arial, sans-serif;
  font-size: 10pt;
  color: #111;
}
h1 { font-size: 18pt; margin: 0 0 0.05in 0; }
h2 {
  font-size: 12pt;
  margin: 0.3in 0 0.08in 0;
  border-bottom: 1px solid #ccc;
  padding-bottom: 0.04in;
}
.subtitle { color: #555; font-size: 10pt; margin-bottom: 0.2in; }
table { width: 100%; border-collapse: collapse; margin-bottom: 0.1in; }
th, td {
  text-align: left;
  padding: 0.04in 0.08in;
  border-bottom: 1px solid #ddd;
  font-size: 9pt;
  vertical-align: top;
}
th { background: #f2f2f2; font-weight: 700; }
tr.total td { font-weight: 700; border-top: 2px solid #999; }
.header-grid { display: flex; flex-wrap: wrap; gap: 0.15in 0.4in; margin-bottom: 0.1in; }
.header-grid .field { min-width: 2in; }
.header-grid .label { font-size: 8pt; color: #666; text-transform: uppercase; }
.header-grid .value { font-size: 10pt; }
.budget-cards { display: flex; gap: 0.2in; margin-bottom: 0.1in; }
.budget-cards .card { flex: 1; border: 1px solid #ddd; border-radius: 4px; padding: 0.1in; }
.budget-cards .card .label { font-size: 8pt; color: #666; text-transform: uppercase; }
.budget-cards .card .value { font-size: 13pt; font-weight: 700; }
.empty { color: #777; font-style: italic; padding: 0.1in 0; }
.asset-photo { width: 0.5in; height: 0.5in; object-fit: cover; border-radius: 3px; }
/* Invoice scans need to be legible (code review finding), unlike the small
   asset-inventory thumbnail above -- sized for actual reading, not a
   fixed-square crop, and kept off a page split. */
.invoice-scan {
  display: block;
  /* Relative to the full-width `.invoice-entry` block (never a narrow table
     cell -- see `_appendix_html`), so this can never demand more than the
     page content box regardless of the @page margins above. A fixed inch
     value here previously had to be hand-kept in sync with those margins
     and, worse, blew up the table's auto-layout width when the image sat
     in a table cell next to other text columns (WeasyPrint expands table
     columns to fit an image's max-width rather than shrinking it -- the
     appendix overflow bug this replaces). */
  max-width: 100%;
  height: auto;
  object-fit: contain;
  border-radius: 3px;
  margin: 0.05in 0;
  page-break-inside: avoid;
}
.invoice-list { margin-bottom: 0.1in; }
.invoice-entry { margin-bottom: 0.15in; page-break-inside: avoid; }
.invoice-caption { font-size: 9pt; }
.section { page-break-inside: avoid; }
"""


def _header_html(data: ProjectReportData) -> str:
    def field(label: str, value: str) -> str:
        return (
            f'<div class="field"><div class="label">{_esc(label)}</div>'
            f'<div class="value">{_esc(value) or "&mdash;"}</div></div>'
        )

    return f"""
    <h1>{_esc(data.name)}</h1>
    <div class="subtitle">Project audit report &mdash; {_esc(data.tenant_name)}</div>
    <div class="header-grid">
      {field("Project code", data.code)}
      {field("Funding source", data.funding_source.title() if data.funding_source else "")}
      {field("Sponsor", data.sponsor)}
      {field("Lead", data.lead_name)}
      {field("Start date", data.start_date or "")}
      {field("End date", data.end_date or "")}
      {field("Status", data.status.title() if data.status else "")}
    </div>
    """


def _budget_card(label: str, amount: Decimal | None, currency: str) -> str:
    value = _esc(_money(amount, currency))
    return (
        f'<div class="card"><div class="label">{_esc(label)}</div>'
        f'<div class="value">{value}</div></div>'
    )


def _budget_summary_html(data: ProjectReportData) -> str:
    cards = f"""
    <div class="budget-cards">
      {_budget_card("Budget total", data.budget_total, data.currency)}
      {_budget_card("Total spent", data.spent, data.currency)}
      {_budget_card("Remaining", data.remaining, data.currency)}
    </div>
    """

    if not data.spend_by_category:
        category_table = '<div class="empty">No expenses recorded yet.</div>'
    else:
        rows = "".join(
            f"<tr><td>{_esc(row.category)}</td>"
            f"<td>{_esc(_money(row.total, data.currency))}</td></tr>"
            for row in data.spend_by_category
        )
        category_table = f"""
        <table>
          <thead><tr><th>Category</th><th>Spend</th></tr></thead>
          <tbody>{rows}</tbody>
        </table>
        """

    return f"""
    <div class="section">
      <h2>Budget summary</h2>
      {cards}
      {category_table}
    </div>
    """


def _expense_ledger_html(data: ProjectReportData) -> str:
    if not data.expenses:
        return """
        <div class="section">
          <h2>Itemized expenses</h2>
          <div class="empty">No expenses have been booked against this project.</div>
        </div>
        """

    rows = "".join(
        f"""<tr>
          <td>{_esc(e.date)}</td>
          <td>{_esc(e.category)}</td>
          <td>{_esc(e.vendor)}</td>
          <td>{_esc(e.invoice_number)}</td>
          <td>{_esc(e.description)}</td>
          <td>{_esc(_money(e.amount, data.currency))}</td>
        </tr>"""
        for e in data.expenses
    )
    total = sum((e.amount for e in data.expenses), Decimal("0.00"))
    total_cell = _esc(_money(total, data.currency))
    return f"""
    <div class="section">
      <h2>Itemized expenses</h2>
      <table>
        <thead>
          <tr>
            <th>Date</th><th>Category</th><th>Vendor</th>
            <th>Invoice #</th><th>Description</th><th>Amount</th>
          </tr>
        </thead>
        <tbody>
          {rows}
          <tr class="total"><td colspan="5">Total</td><td>{total_cell}</td></tr>
        </tbody>
      </table>
    </div>
    """


def _asset_inventory_html(data: ProjectReportData) -> str:
    if not data.assets:
        return """
        <div class="section">
          <h2>Asset inventory</h2>
          <div class="empty">No assets are linked to this project.</div>
        </div>
        """

    rows = []
    for a in data.assets:
        photo_cell = (
            f'<img class="asset-photo" src="{a.photo_data_uri}" alt="" />'
            if a.photo_data_uri
            else ""
        )
        rows.append(
            f"""<tr>
              <td>{photo_cell}</td>
              <td>{_esc(a.name)}</td>
              <td>{_esc(a.category)}</td>
              <td>{_esc(a.serial_number)}</td>
              <td>{_esc(_money(a.purchase_cost, data.currency))}</td>
              <td>{_esc(a.status.title() if a.status else "")}</td>
            </tr>"""
        )

    return f"""
    <div class="section">
      <h2>Asset inventory</h2>
      <table>
        <thead>
          <tr>
            <th></th><th>Name</th><th>Category</th>
            <th>Serial</th><th>Purchase cost</th><th>Status</th>
          </tr>
        </thead>
        <tbody>{"".join(rows)}</tbody>
      </table>
    </div>
    """


def _appendix_html(data: ProjectReportData) -> str:
    if data.documents:
        doc_rows = "".join(
            f"<tr><td>{_esc(d.filename)}</td><td>{_esc(d.kind.replace('_', ' ').title())}</td></tr>"
            for d in data.documents
        )
        documents_html = f"""
        <table>
          <thead><tr><th>Filename</th><th>Kind</th></tr></thead>
          <tbody>{doc_rows}</tbody>
        </table>
        """
    else:
        documents_html = '<div class="empty">No project documents on file.</div>'

    if data.invoices:
        has_scans = any(i.scan_data_uri for i in data.invoices)
        if has_scans:
            # An `<img>` must never share a table row with other text
            # columns (that's exactly what caused the appendix to overflow
            # off the right edge of the page -- WeasyPrint's table
            # auto-layout expands the row to fit the image's max-width
            # instead of shrinking it to the available column width). Each
            # invoice gets its own full-width block instead: a caption line
            # followed by the image below it, so the image is always laid
            # out against the full page content width, not a table cell.
            def _invoice_entry(i: InvoiceRow) -> str:
                caption = (
                    f'<div class="invoice-caption"><strong>{_esc(i.expense_label)}</strong>'
                    f" &mdash; {_esc(i.filename)}</div>"
                )
                image = (
                    f'<img class="invoice-scan" src="{i.scan_data_uri}" alt="" />'
                    if i.scan_data_uri
                    else ""
                )
                return f'<div class="invoice-entry">{caption}{image}</div>'

            invoices_html = (
                f'<div class="invoice-list">{"".join(_invoice_entry(i) for i in data.invoices)}</div>'
            )
        else:
            invoice_rows = "".join(
                f"<tr><td>{_esc(i.expense_label)}</td><td>{_esc(i.filename)}</td></tr>"
                for i in data.invoices
            )
            invoices_html = f"""
            <table>
              <thead><tr><th>Expense</th><th>Invoice scan filename</th></tr></thead>
              <tbody>{invoice_rows}</tbody>
            </table>
            """
    else:
        invoices_html = '<div class="empty">No invoice scans on file.</div>'

    return f"""
    <div class="section">
      <h2>Documents &amp; invoice scans (appendix)</h2>
      <p><strong>Project documents</strong></p>
      {documents_html}
      <p><strong>Expense invoice scans</strong></p>
      {invoices_html}
    </div>
    """


def render_project_report_html(data: ProjectReportData) -> str:
    """Build the full report HTML document — split out from
    `render_project_report_pdf` so a test can assert on the pre-WeasyPrint
    HTML directly (CLAUDE.md/task instructions: cheaper and more precise
    than parsing rendered PDF bytes).
    """
    return f"""<!DOCTYPE html>
    <html>
    <head>
      <meta charset="utf-8" />
      <title>{_esc(data.name)} - Project Report</title>
      <style>{_REPORT_CSS}</style>
    </head>
    <body>
      {_header_html(data)}
      {_budget_summary_html(data)}
      {_expense_ledger_html(data)}
      {_asset_inventory_html(data)}
      {_appendix_html(data)}
    </body>
    </html>
    """


def render_project_report_pdf(data: ProjectReportData) -> bytes:
    """Render `data` to PDF bytes via WeasyPrint (same library/technique as
    `apps.labels.rendering.render_labels_pdf`), then — when
    `data.document_files` is non-empty (only true when the caller passed
    `include_project_documents=True` to `resolve_project_report_data`) —
    append each project document's FULL PAGES via `_append_project_documents`
    below. The WeasyPrint render itself never changes based on this flag;
    the appendix table (`_appendix_html`) already lists every document by
    filename/kind regardless, so this is purely additive post-processing on
    the rendered bytes.
    """
    document_html = render_project_report_html(data)
    buffer = BytesIO()
    HTML(string=document_html).write_pdf(buffer)
    pdf_bytes = buffer.getvalue()

    if data.document_files:
        pdf_bytes = _append_project_documents(pdf_bytes, data.document_files)

    return pdf_bytes


# --- Full-page project document appends (post-WeasyPrint merge step) --------

# Judgment call (task spec: "reasonable ceiling"): a single project document
# beyond this many pages is skipped (falls back to metadata-only, same as an
# unsupported content-type) rather than merged in full, to bound worst-case
# report render time/size against one pathological upload -- a full
# 100-page document is already a very generous "whole progress report",
# well beyond anything this lab actually produces.
_MAX_APPENDED_DOCUMENT_PAGES = 100

# Matches `@page { size: letter portrait; }` in `_REPORT_CSS` above (8.5in x
# 11in at 72pt/in) so the divider page fitz inserts is the same size as
# every WeasyPrint-rendered page around it.
_LETTER_WIDTH_PT = 8.5 * 72
_LETTER_HEIGHT_PT = 11 * 72


def _divider_page_pdf_bytes(label: str) -> bytes:
    """One plain-text divider page (task spec: "keep it simple, plain text
    is fine, doesn't need WeasyPrint styling") labeling the document that
    follows it, built directly with fitz rather than round-tripping through
    WeasyPrint -- this merge step runs entirely on raw PDF bytes, no HTML
    involved.
    """
    import fitz

    doc = fitz.open()
    try:
        page = doc.new_page(width=_LETTER_WIDTH_PT, height=_LETTER_HEIGHT_PT)
        # A generous inset box well within the page bounds on every side --
        # explicitly checked by tests (CLAUDE.md: "confirm your divider-page
        # text/layout doesn't itself overflow the page bounds", given the
        # appendix table-overflow bug this report already had to fix once).
        margin = 72  # 1in
        rect = fitz.Rect(margin, margin, _LETTER_WIDTH_PT - margin, _LETTER_HEIGHT_PT - margin)
        page.insert_textbox(
            rect,
            f"Project document\n\n{label}",
            fontsize=16,
            fontname="helv",
            align=fitz.TEXT_ALIGN_LEFT,
        )
        return doc.tobytes()
    finally:
        doc.close()


def _image_bytes_to_single_page_pdf(raw_bytes: bytes) -> bytes | None:
    """Best-effort: decode an image and re-encode it as a single-page PDF
    via Pillow, which `fitz.open` can then merge like any other PDF. Returns
    `None` (never raises) on any decode failure -- same "swallow it, this
    is best-effort" posture as `apps.projects.services._invoice_scan_data_uri`.

    By the time `document_file.raw_bytes` reaches here, `apps.projects.
    services._project_document_file` has already downscaled it through
    `_resize_and_encode_png` (same 1600px-longest-edge cap the invoice-scan
    path uses, code-review finding: a 25MB phone photo must not embed at
    full resolution) and normalized `content_type` to `image/png` -- this
    function itself no longer needs to decode HEIC/HEIF specifically since
    it only ever receives already-normalized PNG bytes via that path, but
    stays a generic Pillow-openable decoder rather than assuming PNG, in
    case a future caller feeds it something else.
    """
    from PIL import Image

    try:
        with Image.open(BytesIO(raw_bytes)) as img:
            img.load()
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            out = BytesIO()
            img.save(out, format="PDF")
            return out.getvalue()
    except Exception:  # noqa: BLE001 - deliberately broad, see docstring
        return None


def _append_one_document(doc, document_file: DocumentFile) -> None:
    """Append `document_file`'s divider + full pages onto `doc` (an open
    `fitz.Document` for the base rendered report), mutating it in place.
    Swallows every failure for THIS document only (task spec: "reading/
    inserting one bad document must never fail the whole report") -- an
    unsupported content-type, corrupt file, or oversized document is simply
    skipped with no divider and no pages added; the appendix table already
    lists it by filename/kind regardless.
    """
    import fitz

    content_type = document_file.content_type or ""
    try:
        if content_type == "application/pdf":
            source = fitz.open(stream=document_file.raw_bytes, filetype="pdf")
            try:
                if source.page_count == 0 or source.page_count > _MAX_APPENDED_DOCUMENT_PAGES:
                    return
                divider_bytes = _divider_page_pdf_bytes(document_file.label)
                divider = fitz.open(stream=divider_bytes, filetype="pdf")
                try:
                    doc.insert_pdf(divider)
                finally:
                    divider.close()
                doc.insert_pdf(source)
            finally:
                source.close()
            return

        if content_type in PHOTO_CONTENT_TYPES:
            single_page_pdf = _image_bytes_to_single_page_pdf(document_file.raw_bytes)
            if not single_page_pdf:
                return
            source = fitz.open(stream=single_page_pdf, filetype="pdf")
            try:
                if source.page_count == 0 or source.page_count > _MAX_APPENDED_DOCUMENT_PAGES:
                    return
                divider_bytes = _divider_page_pdf_bytes(document_file.label)
                divider = fitz.open(stream=divider_bytes, filetype="pdf")
                try:
                    doc.insert_pdf(divider)
                finally:
                    divider.close()
                doc.insert_pdf(source)
            finally:
                source.close()
            return

        # Anything else (DOCX/XLS/TXT, or an unrecognized content-type):
        # no preview is possible -- skip entirely, same as an unsupported
        # invoice-scan content-type falls back to filename-only.
    except Exception:  # noqa: BLE001 - deliberately broad, see docstring
        return


def _append_project_documents(pdf_bytes: bytes, document_files: list[DocumentFile]) -> bytes:
    """Open the base WeasyPrint-rendered `pdf_bytes` with fitz and append
    each project document's divider + full pages, in order. The whole step
    is best-effort at the OUTER level too: if opening the base PDF itself
    somehow fails, the original `pdf_bytes` is returned unchanged rather
    than raising and failing the entire report job over a documents-appendix
    feature.
    """
    import fitz

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:  # noqa: BLE001 - see docstring
        return pdf_bytes

    try:
        for document_file in document_files:
            _append_one_document(doc, document_file)
        return doc.tobytes()
    finally:
        doc.close()
