# M7 — Project & Grant Management (budgets, expenses, documents, reporting)

> Post-MVP feature milestone. Adds a **project hub** on top of the existing
> thin `Project` config so a lab lead can manage a funded grant end-to-end:
> budget vs. spend, an itemized expense/invoice ledger, project documents
> (proposal/contract/progress reports), and audit-ready PDF + spreadsheet
> exports — **without shifting the app away from its asset-first focus.** A
> project is an *optional lens* over assets and spending, not a new center of
> gravity.

## Why / user story
A team lead runs funded projects (internal or external grants). At audit and
project closure they must produce evidence of every purchase and cost against
the awarded budget: invoices, asset photos, asset/expense types, and how much
budget remains. They want it all tracked in one place and one-click exportable
as a structured PDF report and a field-selectable spreadsheet.

## Scope decisions (confirmed with the user)
- **Full expense ledger**: `Expense` is a first-class record for ANY cost
  (asset, consumable, shipping, service, software, travel), each with its own
  invoice attachment; an asset purchase MAY link to its `Expense`.
- **Budget = single total + spend categories**: one `budget_total` per project;
  each expense carries an `ExpenseCategory` so the report breaks spend down by
  category. (No per-category allocations in this milestone.)
- **Financial access = ProjectLead-scoped-to-own-project, Admin tenant-wide**,
  reusing the existing union-of-memberships scope rule (`rbac.md` §1/§3).

## Non-negotiables (same as every milestone — see CLAUDE.md)
- Every new tenant-owned table is tenant-scoped centrally, has an RLS policy
  (fail-closed), and composite `(tenant, project)` indexes. `Expense`,
  `ProjectDocument`, and the invoice/document `Attachment` rows are all
  **project-scoped** so the RBAC scope rule and RLS both apply.
- RBAC server-side on every endpoint, after tenant isolation, with the new
  permission keys below. UI gating is never the boundary.
- Every mutating action is audited (immutable before/after).
- Report PDF render runs in **Celery** (reuses the labels WeasyPrint pipeline),
  never in the request cycle. CSV export streams (reuses the assets export
  technique).
- Attachment binaries live on the storage backend; only `storage_key` +
  metadata in the DB — reuse `apps.assets.services.save_attachment_file`.

## Data model (see `data-model.md` §2 additions)
- **Project** (extend `projects_project`, additive/nullable):
  `code`, `funding_source (internal|external)`, `sponsor`, `start_date`,
  `end_date`, `budget_total (Decimal 14,2)`, `currency (3)`,
  `status (active|closed)`, `description`. Existing `name/lead_user/is_active`
  unchanged.
- **ExpenseCategory** (`projects_expense_category`, tenant-wide config like
  `Location`): `name`, `is_active`. Seeded defaults: Equipment, Consumables,
  Services, Software, Travel, Shipping, Other.
- **Expense** (`projects_expense`, tenant + **project**-scoped): `project`,
  `category (SET_NULL)`, `amount (Decimal 14,2)`, `currency (3)`, `date`,
  `vendor`, `invoice_number`, `description`, `asset (nullable, SET_NULL)`,
  `created_by (SET_NULL)`, timestamps.
- **ProjectDocument** (`projects_project_document`, tenant + project-scoped):
  `project`, `kind (proposal|contract|progress_report|other)`, plus the same
  storage fields as `Attachment` (`storage_key, filename, content_type, size,
  uploaded_by`).
- **ExpenseAttachment** (`projects_expense_attachment`, invoice scans).
  **Decision (db-migration-specialist, T7.1): a dedicated table, NOT a reused
  generic attachment.** An invoice scan anchors to an `Expense` (FK CASCADE)
  while `ProjectDocument` anchors to a `Project`; one generic table would need
  two nullable, mutually-exclusive parent FKs, losing the NOT NULL + CASCADE
  integrity that keeps every attachment strictly project-scoped through exactly
  one parent and muddying the `(tenant, <parent>)` index. `apps.assets` already
  follows this "one dedicated attachment table per anchor" pattern
  (`assets.Attachment` -> `Asset`), so a peer table keeps the codebase
  consistent and each table's RLS/index story clean. Same storage fields as
  `ProjectDocument`/`Attachment` (binary on the storage backend, only
  `storage_key` + metadata in the DB); index `(tenant, expense)`.

## RBAC (see `rbac.md` §3 additions)
New keys: `project.view`, `project.manage`, `expense.view`, `expense.manage`.
Matrix (🟡 = only within a project the user leads):
| key | Admin | Project Lead | Member | Viewer |
|---|---|---|---|---|
| project.view | ✅ | 🟡 | ✅ (own tenant, no financials unless granted) | ✅ |
| project.manage | ✅ | 🟡 | ➖ | ➖ |
| expense.view | ✅ | 🟡 | ➖ | ➖ |
| expense.manage | ✅ | 🟡 | ➖ | ➖ |
Budget/expense endpoints evaluate the 🟡 keys against project-scoped
memberships only (`rbac.services.get_effective_permissions`). Project structural
edits (existing `/projects` CRUD create/delete) stay Admin-gated (`tenant.manage`).

**Product decision (code-review pass, backend Slice 2): financials AND grant
documents are restricted to that project's Lead (scoped) + Admins only — i.e.
`expense.view` scoped to the SPECIFIC project, not the tenant-wide-grantable
`project.view` row above.** This sharpens the "no financials unless granted"
parenthetical into an enforced boundary rather than a UI-only convention:
- `GET /projects/{id}` still 200s for anyone holding `project.view` (Member/
  Viewer tenant-wide, per the row above — the project itself is visible), but
  `budget_total`/`spent`/`remaining`/`spend_by_category` are redacted to
  `null` unless the caller ALSO holds `expense.view` scoped to that project
  (`apps.projects.serializers.ProjectSerializer._can_view_financials`).
- `GET/POST /projects/{id}/documents` (proposal/contract/progress_report —
  these routinely restate the exact budget figures redacted above) is gated
  by `expense.view` for reads too (not `project.view`), so a plain Member/
  Viewer or a Lead of a DIFFERENT project gets a 403 on the whole
  sub-resource, not a redacted/empty list (`apps.projects.permissions.
  _action_permission_key`). Writes stay `project.manage`-gated, unchanged.

## API (`/api/v1`, RFC-7807 errors — see `api-and-ui.md`)
- `GET/PATCH /projects/{id}` — detail incl. budget rollup (`budget_total`,
  `spent`, `remaining`, `spend_by_category[]`). Create/delete stay Admin.
- `GET /projects/{id}/assets` — reuses `apps.assets.api.visible_assets_queryset`
  filtered to the project; same pagination/serializer as the asset list.
- `GET/POST /projects/{id}/expenses`, `GET/PATCH/DELETE /expenses/{id}`.
- `POST /expenses/{id}/attachment`, `GET /expenses/{id}/attachment` (invoice).
- `GET/POST /projects/{id}/documents`, `DELETE /documents/{id}`.
- `POST /projects/{id}/report` → async job (jobs poller) → `report.pdf`.
- `GET /projects/{id}/export.csv?fields=...` — streamed, field-selectable
  expense+asset export, RBAC-scoped like the asset export.

## Sequencing (vertical slices — each verified before the next)
1. **Data layer** (db-migration-specialist): models + migrations + RLS +
   indexes + seed categories; run up AND down on scratch Postgres.
2. **Backend API** (backend-engineer): RBAC keys + seed grant, serializers,
   viewsets, budget rollup, uploads, project-assets, export. Follow
   `add-endpoint` skill.
3. **Verify** (qa-test-engineer → code-reviewer): tenant isolation, RBAC scope
   (ProjectLead can't see another project's expenses/budget), audit, query
   budgets, RLS fires as `cortex_app`.
4. **Report PDF** (pwa-scan-specialist): WeasyPrint template + Celery task.
5. **Frontend** (frontend-engineer): Projects nav tab + hub (Overview/Assets/
   Expenses/Documents/Report) + forms. Follow `add-screen` skill.
6. **Final verify** (qa → code-reviewer) + docs (CHANGELOG, data-model, rbac,
   api-and-ui, README).

## Exit criteria (acceptance)
- A ProjectLead opens their project, sees budget total, spent, and remaining
  with a per-category breakdown; adds an expense with an invoice scan; uploads
  a proposal/contract/progress report; the project's assets list through the
  hub; one click produces a structured PDF report and a field-selectable CSV.
- A ProjectLead on project A gets 403 (server-side) for project B's expenses,
  budget, and documents — proven by test, not UI gating.
- Existing asset-first flows are unchanged; a project remains optional
  (`Asset.project_id` NULL = general pool as today).
