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
class InvoiceRow:
    expense_label: str  # e.g. "2026-01-15 - Acme Corp - $1,200.00"
    filename: str


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
    `apps.labels.rendering.render_labels_pdf`)."""
    document_html = render_project_report_html(data)
    buffer = BytesIO()
    HTML(string=document_html).write_pdf(buffer)
    return buffer.getvalue()
