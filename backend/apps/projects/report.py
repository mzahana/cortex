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
    """ONE EXPENSE — i.e. one order from a vendor — not one item on it.

    This table used to list every item line, so an order containing eight
    parts produced eight rows and the reader had to add them back up to find
    what the order cost. The user's model is the one that matters here: an
    expense IS an order, and its items are detail. They are set out per order
    in the "Orders and invoices" section below, which is where itemization
    belongs.
    """

    #: The order's number, matching the Orders section, the Expenses tab and
    #: the downloaded pack filename. Zero for a pre-M8 standalone expense that
    #: has no order behind it — rendered as a dash, not a blank.
    seq: int
    date: str  # ISO 8601, already resolved server-side
    vendor: str
    item_count: int
    amount: Decimal  # items as entered, before shipping and tax
    overhead: Decimal  # the order's shipping + tax
    loaded: Decimal  # amount + overhead — what the order actually cost
    #: What the bank was charged. Normally equal to `loaded`; when it is not,
    #: the difference is money that is not yet itemized, and the order's own
    #: entry below says so explicitly.
    charged: Decimal = Decimal("0.00")


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
class ReconciliationOrderRow:
    """One bank charge and how it breaks down (M8 Phase 3 §6.2).

    This is the section that makes an audit self-explanatory: statement line ->
    receipts -> items, with the paperwork's whereabouts stated. Because an
    order belongs to exactly one project, the whole charge is this project's —
    there is no other-projects remainder to redact.
    """

    paid_on: str
    vendor: str
    amount: str  # what the bank took, with currency
    account_label: str
    statement_ref: str
    items_total: str
    variance_note: str
    is_balanced: bool
    has_statement_scan: bool
    shipments: list["ReconciliationShipmentRow"] = field(default_factory=list)
    #: 1-based position in statement order. Printed as "Charge N" here and on
    #: every ledger line and scan divider that refers back to this charge.
    seq: int = 0
    #: This order's own PDF scans, merged directly beneath its table by
    #: `render_project_report_pdf` rather than pooled at the end of the
    #: report — so the paperwork sits with the order it proves.
    scan_files: list[DocumentFile] = field(default_factory=list)
    #: This order's image scans, embedded inline in its block for the same
    #: reason. Every piece of paperwork belonging to an order renders with
    #: that order; nothing about a scan's file format changes where it lands.
    image_scans: list[InvoiceRow] = field(default_factory=list)


@dataclass(frozen=True)
class ReconciliationShipmentRow:
    label: str
    receipt_number: str
    total: str  # in the receipt's own currency
    converted: str  # "" when it needs no conversion
    item_count: int
    has_receipt_scan: bool
    #: Shipping and tax are shown as their OWN rows, not folded into the
    #: receipt total or relegated to a footnote — otherwise a reader cannot
    #: check that items + shipping + tax = what the receipt says, which is the
    #: single arithmetic an auditor actually performs. Same reason the expense
    #: pack renders them as rows (`apps.finance.pack`).
    items_subtotal: str = ""
    shipping: str = ""
    tax: str = ""


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
    #: Every scan in the report, in order. Kept as the complete inventory of
    #: paperwork; rendering happens per-order, not from this list.
    invoices: list[InvoiceRow] = field(default_factory=list)
    #: The subset belonging to no order (pre-M8 expense attachments whose
    #: expense was never linked to a receipt). They have no order block to sit
    #: under, so they are the only scans still listed in the appendix.
    orphan_invoices: list[InvoiceRow] = field(default_factory=list)
    #: M8 §6.2 — the statement-to-receipt-to-item trail, per bank charge.
    reconciliation: list[ReconciliationOrderRow] = field(default_factory=list)

    # Only populated when `include_project_documents=True` was passed to
    # `resolve_project_report_data` — the raw bytes backing the full-page
    # appends `_append_project_documents` does after the WeasyPrint render.
    # Empty list (not None) when the flag is off, mirroring every other
    # list field's "empty means nothing to render" convention here.
    document_files: list[DocumentFile] = field(default_factory=list)

    # Raw bytes for PDF receipt/statement scans, appended in full by
    # `render_project_report_pdf`. Only populated when
    # `include_invoice_scans=True`; image scans never land here because they
    # embed inline instead (see `InvoiceRow.scan_data_uri`).
    scan_files: list[DocumentFile] = field(default_factory=list)


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
/* M8 §6.2 reconciliation section */
.recon { margin: 0 0 5mm 0; page-break-inside: avoid; }
.recon-head { font-weight: bold; margin: 0; }
.recon-sub { margin: 0 0 1mm 0; font-size: 8pt; color: #555; }
.recon-ok { color: #1a7f37; font-weight: bold; }
.recon-warn { color: #b8860b; font-weight: bold; }
/* The shipping/tax breakdown under each receipt: visibly subordinate to the
   receipt's own row, but a full row of its own -- small grey text was the
   original complaint ("not clearly visible ... I want each as a row so the
   sum is clear"). */
tr.breakdown td { color: #444; border-bottom: none; padding-top: 0.4mm;
                  padding-bottom: 0.4mm; }
.recon-scan-note { font-size: 8pt; color: #555; font-style: italic;
                   margin: 1mm 0 0 0; }

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
    """One row per expense (order), not per item — see `ExpenseRow`."""
    if not data.expenses:
        return """
        <div class="section">
          <h2>Expenses</h2>
          <div class="empty">No expenses have been booked against this project.</div>
        </div>
        """

    def _row(e: ExpenseRow) -> str:
        # An unreconciled order is called out here rather than only in its own
        # entry below: this table is the one many readers stop at.
        mismatch = e.charged != e.loaded
        charged_cell = (
            f'<td class="recon-warn">{_esc(_money(e.charged, data.currency))}</td>'
            if mismatch
            else f"<td>{_esc(_money(e.charged, data.currency))}</td>"
        )
        return f"""<tr>
          <td>{e.seq if e.seq else '&mdash;'}</td>
          <td>{_esc(e.date)}</td>
          <td>{_esc(e.vendor)}</td>
          <td>{e.item_count}</td>
          <td>{_esc(_money(e.amount, data.currency))}</td>
          <td>{_esc(_money(e.overhead, data.currency))}</td>
          <td>{_esc(_money(e.loaded, data.currency))}</td>
          {charged_cell}
        </tr>"""

    rows = "".join(_row(e) for e in data.expenses)
    item_total = sum((e.amount for e in data.expenses), Decimal("0.00"))
    overhead_total = sum((e.overhead for e in data.expenses), Decimal("0.00"))
    loaded_total = sum((e.loaded for e in data.expenses), Decimal("0.00"))
    charged_total = sum((e.charged for e in data.expenses), Decimal("0.00"))
    return f"""
    <div class="section">
      <h2>Expenses</h2>
      <p class="muted">One row per expense &mdash; an order placed with a
      vendor. <strong>Total cost</strong> is what the order cost the project
      (its items plus shipping and tax) and is the figure that sums to the
      spend above; <strong>charged</strong> is what the bank actually took.
      Where those differ, the money is not yet fully itemized and the order&rsquo;s
      own entry says so. Every order&rsquo;s items, receipts and scans are set out
      in &ldquo;{html.escape(CHARGES_SECTION_TITLE)}&rdquo; below.</p>
      <table>
        <thead>
          <tr>
            <th>#</th><th>Date</th><th>Vendor</th><th>Items</th>
            <th>Item cost</th><th>Shipping &amp; tax</th>
            <th>Total cost</th><th>Charged</th>
          </tr>
        </thead>
        <tbody>
          {rows}
          <tr class="total"><td colspan="4">Total</td>
            <td>{_esc(_money(item_total, data.currency))}</td>
            <td>{_esc(_money(overhead_total, data.currency))}</td>
            <td>{_esc(_money(loaded_total, data.currency))}</td>
            <td>{_esc(_money(charged_total, data.currency))}</td></tr>
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


def _shipment_rows(s: ReconciliationShipmentRow) -> str:
    """One receipt, then the arithmetic that produces its total.

    The breakdown rows are emitted even when shipping/tax are zero: "Shipping
    0.00" is information (it was checked and there was none), whereas an absent
    row leaves the reader unable to tell whether shipping was zero or simply
    never recorded. That ambiguity is exactly what made an earlier version of
    this section read as if the numbers might not add up.
    """
    header = (
        f"<tr><td><strong>{html.escape(s.label)}"
        f"{(' · ' + html.escape(s.receipt_number)) if s.receipt_number else ''}</strong></td>"
        f"<td class='num'><strong>{html.escape(s.total)}</strong></td>"
        f"<td class='num'>{html.escape(s.converted)}</td>"
        f"<td class='num'>{s.item_count}</td>"
        f"<td>{'on file' if s.has_receipt_scan else '&mdash;'}</td></tr>"
    )
    if not s.items_subtotal:
        return header

    def _line(label: str, value: str) -> str:
        return (
            f"<tr class='breakdown'><td>{label}</td>"
            f"<td class='num'>{html.escape(value)}</td>"
            f"<td colspan='3'></td></tr>"
        )

    return (
        header
        + _line("&nbsp;&nbsp;Items subtotal", s.items_subtotal)
        + _line("&nbsp;&nbsp;Shipping", s.shipping)
        + _line("&nbsp;&nbsp;Tax", s.tax)
    )


#: Plain name for the section. "Reconciliation — bank charges" was accounting
#: vocabulary for what is, in this system, simply the list of orders and the
#: invoices behind them.
CHARGES_SECTION_TITLE = "Orders and invoices"


def _charge_block_html(order: ReconciliationOrderRow) -> str:
    """One order: its table, its image scans inline, and a pointer to the PDF
    scans merged on the pages that follow.

    Rendered as a standalone fragment rather than as part of one long section
    so `render_project_report_pdf` can merge each order's receipt pages
    directly beneath it. Grouping the paperwork with the order it proves is
    the whole point — a reader should never have to page to the back of the
    document and match receipts up by amount.
    """
    shipments = (
        "".join(_shipment_rows(s) for s in order.shipments)
        or "<tr><td colspan='5'>No shipments recorded.</td></tr>"
    )
    state_class = "recon-ok" if order.is_balanced else "recon-warn"
    account = f" &mdash; {html.escape(order.account_label)}" if order.account_label else ""
    ref = f" &mdash; ref {html.escape(order.statement_ref)}" if order.statement_ref else ""
    scan_note = (
        '<p class="recon-scan-note">The scanned paperwork for this order '
        "follows immediately after this page.</p>"
        if order.scan_files
        else ""
    )
    images = "".join(
        f'<div class="invoice-entry">'
        f'<div class="invoice-caption"><strong>{_esc(i.expense_label)}</strong>'
        f" &mdash; {_esc(i.filename)}</div>"
        f'<img class="invoice-scan" src="{i.scan_data_uri}" alt="" />'
        f"</div>"
        for i in order.image_scans
        if i.scan_data_uri
    )
    return f"""
            <div class="recon">
              <p class="recon-head">
                Order {order.seq} &mdash; {html.escape(order.paid_on)}
                &mdash; {html.escape(order.vendor or 'Unknown vendor')}
                &mdash; {html.escape(order.amount)}
                {account}{ref}
              </p>
              <p class="recon-sub">
                Statement scan: {'on file' if order.has_statement_scan else 'not attached'}
                &middot; Items total {html.escape(order.items_total)}
                &middot; <span class="{state_class}">{html.escape(order.variance_note)}</span>
              </p>
              <table>
                <thead><tr><th>Shipment</th><th class="num">Receipt total</th>
                  <th class="num">Charged</th><th class="num">Items</th>
                  <th>Receipt scan</th></tr></thead>
                <tbody>{shipments}</tbody>
              </table>
              {images}
              {scan_note}
            </div>
            """


def _charges_intro_html() -> str:
    return f"""
    <h2>{html.escape(CHARGES_SECTION_TITLE)}</h2>
    <p class="muted">One entry per order: what the bank was charged, the
    receipts it paid for, and the scanned paperwork proving it &mdash; each
    order&rsquo;s scans sit directly beneath that order.</p>
    """


def _reconciliation_html(data: ProjectReportData) -> str:
    """The whole charges section as ONE fragment.

    Used for the HTML-only path (tests, and any caller wanting the section
    without merged scan pages). `render_project_report_pdf` does NOT use this
    — it renders each charge separately so the scans can be interleaved.
    """
    if not data.reconciliation:
        return ""
    blocks = "".join(_charge_block_html(order) for order in data.reconciliation)
    return _charges_intro_html() + blocks


def _appendix_html(data: ProjectReportData) -> str:
    """Project documents only.

    Receipts and bank statements deliberately do NOT appear here. They used to
    be listed in this appendix, separated from the orders they belong to, which
    meant a reader had to match paperwork to spending by amount — they now
    render inside their own order block (`_charge_block_html`), images inline
    and PDFs merged as full pages directly beneath. An order's proof belongs
    with the order, not in a pile at the back of the document.
    """
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

    if data.orphan_invoices:
        if any(i.scan_data_uri for i in data.orphan_invoices):
            # An `<img>` must never share a table row with other text columns
            # (that is what once pushed this appendix off the right edge of the
            # page — WeasyPrint's table auto-layout grows the row to the image's
            # max-width instead of shrinking the image). Each scan gets its own
            # full-width block: caption, then image beneath it.
            entries = "".join(
                f'<div class="invoice-entry">'
                f'<div class="invoice-caption"><strong>{_esc(i.expense_label)}</strong>'
                f" &mdash; {_esc(i.filename)}</div>"
                + (
                    f'<img class="invoice-scan" src="{i.scan_data_uri}" alt="" />'
                    if i.scan_data_uri
                    else ""
                )
                + "</div>"
                for i in data.orphan_invoices
            )
            listing = f'<div class="invoice-list">{entries}</div>'
        else:
            rows = "".join(
                f"<tr><td>{_esc(i.expense_label)}</td><td>{_esc(i.filename)}</td></tr>"
                for i in data.orphan_invoices
            )
            listing = f"""
            <table>
              <thead><tr><th>Expense</th><th>Scan filename</th></tr></thead>
              <tbody>{rows}</tbody>
            </table>
            """
        orphans_html = (
            "<p><strong>Other scans (not linked to an order)</strong></p>" + listing
        )
    else:
        orphans_html = ""

    return f"""
    <div class="section">
      <h2>Project documents (appendix)</h2>
      {documents_html}
      {orphans_html}
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
      {_reconciliation_html(data)}
      {_appendix_html(data)}
    </body>
    </html>
    """


def _wrap_fragment(title: str, fragment: str) -> str:
    """A minimal standalone HTML document around one section fragment, sharing
    the report's stylesheet so a separately-rendered charge block is
    typographically identical to the rest of the document."""
    return f"""<!DOCTYPE html>
    <html><head><meta charset="utf-8" /><title>{_esc(title)}</title>
    <style>{_REPORT_CSS}</style></head>
    <body>{fragment}</body></html>
    """


def _render_fragment(title: str, fragment: str) -> bytes:
    buffer = BytesIO()
    HTML(string=_wrap_fragment(title, fragment)).write_pdf(buffer)
    return buffer.getvalue()


def render_project_report_pdf(data: ProjectReportData) -> bytes:
    """Render `data` to PDF bytes, interleaving each bank charge's scanned
    paperwork directly beneath that charge.

    The document is assembled in pieces rather than as one WeasyPrint render:

    1. the main body (header, budget, ledger, asset inventory);
    2. then, per charge: that charge's table, immediately followed by the full
       pages of its receipts and bank statement;
    3. then the appendix, any scans belonging to no charge (pre-M8 expense
       attachments), and finally project documents.

    Step 2 is why the pieces exist. Scans are merged PDF pages, so they can
    only land at a page boundary — the only way to place them *with* their
    charge instead of pooled at the end of the report is to end the charge's
    own page there. The cost is that each charge starts a new page; the
    benefit is that the paperwork proving a charge sits with it, which is how
    an audit pack is expected to read.
    """
    parts: list[bytes] = []

    body = (
        f"{_header_html(data)}{_budget_summary_html(data)}"
        f"{_expense_ledger_html(data)}{_asset_inventory_html(data)}"
    )
    parts.append(_render_fragment(data.name, body))

    if data.reconciliation:
        parts.append(_render_fragment(data.name, _charges_intro_html()))
        for order in data.reconciliation:
            parts.append(_render_fragment(data.name, _charge_block_html(order)))
            for scan in order.scan_files:
                parts.append(_one_document_pdf(scan, heading=f"Order {order.seq}"))

    parts.append(_render_fragment(data.name, _appendix_html(data)))

    pdf_bytes = _concatenate(parts)

    # Scans belonging to no charge (pre-M8 expense attachments) have nothing to
    # sit beneath, so they keep the old pooled placement at the end.
    if data.scan_files:
        pdf_bytes = _append_project_documents(
            pdf_bytes, data.scan_files, heading="Receipt / bank statement"
        )

    if data.document_files:
        pdf_bytes = _append_project_documents(pdf_bytes, data.document_files)

    return pdf_bytes


def _concatenate(parts: list[bytes]) -> bytes:
    """Join rendered PDF fragments in order.

    Best-effort per part, consistent with every other merge step here: a
    fragment that will not open is skipped rather than costing the document.
    """
    import fitz

    out = fitz.open()
    try:
        for part in parts:
            if not part:
                continue
            try:
                source = fitz.open(stream=part, filetype="pdf")
            except Exception:  # noqa: BLE001 - see docstring
                continue
            try:
                out.insert_pdf(source)
            finally:
                source.close()
        return out.tobytes()
    finally:
        out.close()


def _one_document_pdf(document_file: DocumentFile, *, heading: str) -> bytes:
    """A divider page plus one document's full pages, as standalone bytes —
    the same work `_append_one_document` does, but producing a fragment that
    can be positioned mid-report instead of appended at the end."""
    import fitz

    doc = fitz.open()
    try:
        _append_one_document(doc, document_file, heading)
        return doc.tobytes()
    except Exception:  # noqa: BLE001 - one bad scan must not cost the report
        return b""
    finally:
        doc.close()


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


def _divider_page_pdf_bytes(label: str, heading: str = "Project document") -> bytes:
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
            f"{heading}\n\n{label}",
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
        with Image.open(BytesIO(raw_bytes)) as opened:
            opened.load()
            # `convert()` returns a plain Image, not the ImageFile `open()`
            # gives back — bind it to its own name so the two stay distinct.
            img = opened.convert("RGB") if opened.mode not in ("RGB", "L") else opened
            out = BytesIO()
            img.save(out, format="PDF")
            return out.getvalue()
    except Exception:  # noqa: BLE001 - deliberately broad, see docstring
        return None


def _append_one_document(doc, document_file: DocumentFile, heading: str) -> None:
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
                divider_bytes = _divider_page_pdf_bytes(document_file.label, heading)
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
                divider_bytes = _divider_page_pdf_bytes(document_file.label, heading)
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


def _append_project_documents(
    pdf_bytes: bytes,
    document_files: list[DocumentFile],
    heading: str = "Project document",
) -> bytes:
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
            _append_one_document(doc, document_file, heading)
        return doc.tobytes()
    finally:
        doc.close()
