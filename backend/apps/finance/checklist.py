"""The **audit-readiness checklist** (M8 Phase 3 §6.3).

The other reports describe what happened. This one describes what is *missing* —
the things an auditor would ask about, listed before they get the chance:

- a charge whose items do not add up to what the bank took;
- a charge with no bank statement attached;
- a shipment with no receipt scan;
- an item with no category, so it lands nowhere in the spend breakdown;
- an asset with no photo or no serial number.

It is deliberately a to-do list, not a report card: every finding names the
record and says what to do about it. Cheap to build, because the reconciliation
figures already exist — this only asks the questions.

Follows the same data-in/render-out split as every other PDF here: the resolver
in `apps.finance.services` reads the database, this module only formats.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from io import BytesIO

from weasyprint import HTML

from apps.common.pdf import stamp_pages


@dataclass(frozen=True)
class Finding:
    """One thing to fix. `where` names the record; `fix` says what to do."""

    severity: str  # "blocker" | "advisory"
    what: str
    where: str
    fix: str


@dataclass(frozen=True)
class ChecklistData:
    tenant_name: str
    project_name: str
    generated_note: str = ""
    findings: list[Finding] = field(default_factory=list)

    @property
    def blockers(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "blocker"]

    @property
    def advisories(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "advisory"]


_CSS = """
@page { size: letter portrait; margin: 18mm 16mm 16mm 16mm; }
body { font-family: "DejaVu Sans", sans-serif; font-size: 9pt; color: #111; }
h1 { font-size: 17pt; margin: 0 0 1mm 0; }
h2 { font-size: 11pt; margin: 6mm 0 2mm 0; border-bottom: 1px solid #999;
     padding-bottom: 1mm; }
.sub { color: #555; margin: 0 0 5mm 0; }
table { width: 100%; border-collapse: collapse; margin-top: 2mm; }
th, td { text-align: left; padding: 1.6mm 2mm; border-bottom: 0.4pt solid #ddd;
         vertical-align: top; }
th { background: #f2f2f2; font-size: 8.5pt; }
.clear { color: #1a7f37; font-weight: bold; font-size: 11pt; }
.count { font-weight: bold; }
.foot { margin-top: 8mm; font-size: 7.5pt; color: #666; }
"""


def _rows(findings: list[Finding]) -> str:
    if not findings:
        return "<tr><td colspan='3'>Nothing outstanding.</td></tr>"
    return "".join(
        f"<tr><td>{html.escape(f.what)}</td>"
        f"<td>{html.escape(f.where)}</td>"
        f"<td>{html.escape(f.fix)}</td></tr>"
        for f in findings
    )


def render_checklist_html(data: ChecklistData) -> str:
    blockers = data.blockers
    advisories = data.advisories

    if not data.findings:
        body = (
            "<p class='clear'>Nothing outstanding — every charge is itemized "
            "and every receipt is on file.</p>"
        )
    else:
        body = f"""
        <h2>Would fail an audit ({len(blockers)})</h2>
        <table>
          <thead><tr><th>Issue</th><th>Where</th><th>What to do</th></tr></thead>
          <tbody>{_rows(blockers)}</tbody>
        </table>

        <h2>Worth tidying ({len(advisories)})</h2>
        <table>
          <thead><tr><th>Issue</th><th>Where</th><th>What to do</th></tr></thead>
          <tbody>{_rows(advisories)}</tbody>
        </table>
        """

    return f"""
    <html><head><meta charset="utf-8"><style>{_CSS}</style></head>
    <body>
      <h1>Audit readiness</h1>
      <p class="sub">{html.escape(data.project_name)} · {html.escape(data.tenant_name)}</p>
      {body}
      <p class="foot">{html.escape(data.generated_note)}</p>
    </body></html>
    """


def render_checklist_pdf(data: ChecklistData) -> bytes:
    buffer = BytesIO()
    HTML(string=render_checklist_html(data)).write_pdf(buffer)
    return stamp_pages(buffer.getvalue(), footer=data.generated_note)
