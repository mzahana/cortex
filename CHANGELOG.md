# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Versions map to milestones in `docs/tasks/`: **M0** → `0.1.0`, and subsequent
milestones bump the minor version until the first production release (`1.0.0`).

`frontend/package.json`'s `version` field is the single source of truth for the
version shown in the app UI (footer of the login screen and the sidebar/"More"
sheet) — it is inlined at build time via `vite.config.ts`'s `__APP_VERSION__`
define. **Bump `frontend/package.json`'s `version` to match every new entry
added here** so the displayed version never drifts from the changelog. This is
no longer only a convention: pushing a `vX.Y.Z` tag fails CI's
`version-consistency` job unless the tag, `frontend/package.json`, and a
matching `## [X.Y.Z]` section here all agree. See "Cutting a release" in
`docs/development.md`.

## [Unreleased]

## [0.15.1] - 2026-08-05

### Fixed

- **`migrate` could fail upgrading a database that already had tenants.** The
  RBAC seed data migrations (`rbac.0002`, `rbac.0003`) used the *concrete*
  model classes, which describe today's schema rather than the schema in place
  when the migration runs. On the forward path this aborted the upgrade at
  `rbac.0003` with `column rbac_role.is_customized does not exist` — that
  column is added by `rbac.0004`, one migration *later* — for any install with
  at least one tenant row (a tenant-less database never entered the seed loop,
  which is why CI stayed green). Both migrations now seed through historical
  (`apps.get_model`) models, via a shared unfiltered-queryset helper in
  `apps.rbac.seed`; the runtime seeding path (new-tenant signal) is unchanged.
- **Migration reversibility for the CI/empty-DB case (`migrate up/down/up`).**
  The same two migrations' reverse paths failed a full `migrate rbac zero`
  with `relation "rbac_user_permission_override" does not exist` (the deletion
  collector cascading into a table `0004`'s reverse had already dropped) and
  then `column rbac_role.is_customized does not exist`; they now use historical
  models too. Known pre-existing limitation, *not* addressed here: reversing
  `rbac.0002` on a database that has real role memberships still fails with a
  `ProtectedError`, because `Membership.role` is `on_delete=PROTECT`. Reversal
  remains a CI/development-time gate, not a supported production rollback.

  No schema change in either fix.

## [0.15.0] - 2026-08-05

### Added

- **Admin-editable permissions (Admin → Users & Roles → "Roles & permissions").**
  `docs/rbac.md` §3's matrix is now the *default*, not a hard-coded law: an
  Admin edits any role's permission set from a checkbox matrix (the motivating
  case — a Project Lead who also needs `category.manage`), authors custom roles
  (optionally cloned from an existing one), and resets an edited system role
  back to its shipped defaults. New endpoints: `GET /api/v1/permissions` (the
  fixed permission vocabulary), full CRUD on `/api/v1/roles`, and
  `POST /api/v1/roles/{id}/reset`. Writes require **tenant-wide
  `tenant.manage`** (deliberately stricter than `user.manage`, so a Project
  Lead can't edit the rules that define their own power) and are audited under
  `role.assign` with before/after permission sets.
- **Per-user permission overrides** — a tri-state (Inherit / Always allow /
  Never allow) matrix per user, for the "this one person needs a deviation from
  their role" case, without authoring a whole custom role
  (`GET/PUT /api/v1/users/{id}/permissions`, Admin-only, audited). Overrides
  are tenant-wide by design and "Never allow" beats every role grant, including
  one held only through a project-scoped membership.
- **Lockout guardrail** — any role edit or override that would leave the tenant
  with no active user holding tenant-wide `tenant.manage` + `user.manage` is
  rejected (400) and rolled back. Self-hosted Cortex has no break-glass path.
- **Print a label straight from an asset** — Asset Detail has a "Print label"
  action (gated by `label.generate` for that asset's project) that generates
  and downloads the PDF without a trip to the Labels screen, which still owns
  batch printing. New `single` label template (one 4in × 2in label per page) is
  the default for this action, so a one-off print doesn't waste 29 of 30
  die-cut labels on an Avery sheet.
- **Built-in asset link field** — `Asset.url`, for the product/procurement/docs
  page, so the near-universal case no longer needs a per-category custom field.
  Editable on the asset form, rendered as a link on Asset Detail, and included
  as a core column in CSV export/import (so it round-trips instead of becoming
  a custom field). Only `http`/`https` are accepted, at every write path.
- **Download all of a project's documents as a structured ZIP** —
  `POST /api/v1/projects/{id}/archive` enqueues a Celery job that bundles the
  **original** files (not a rendered PDF): `documents/<kind>/`, `invoices/<expense>/`,
  optionally `assets/<asset>/`, plus `expenses.csv`, a `manifest.csv` covering
  every file (size, uploader, timestamp), and a `README.txt`. Gated on
  `expense.view` scoped to that project and audited. The archive is streamed to
  disk with a hard size cap, so a large project fails the job with an
  actionable message rather than exhausting the worker.
- **Fetch expense details from a linked asset** — the project expense form can
  pull the asset's amount, currency, vendor and purchase date
  (`GET /api/v1/assets/{id}/expense-prefill`) and copy a PO/invoice already
  filed against that asset onto the expense
  (`POST /api/v1/expenses/{id}/attachment-from-asset`, a real byte copy with
  its own storage object). Convenience only — every field stays editable, and
  fetching never overwrites something already typed in. The copy requires
  `expense.manage` on the expense's project **and** `asset.view` on the source
  asset's project.
- **Attachments have a document type** — `invoice` / `receipt` /
  `purchase_order` / `quote` / `warranty` / `manual` / `other`, set when
  uploading and shown as a badge on the asset's tiles. This is deliberately
  orthogonal to the existing `kind` (photo vs document), which is a
  storage/content-type discriminator, not a meaning: a phone photo of a paper
  invoice is `kind="photo"` AND `doc_type="invoice"`.
- **Remove an asset's photos/POs/receipts** — asset attachments could be
  uploaded but never deleted. `DELETE /api/v1/attachments/{id}` removes the
  row **and the stored file**, so the volume actually reclaims the space
  (best-effort on the storage side: an already-missing file never blocks or
  rolls back the DB delete). Gated by `asset.attach` scoped to the owning
  asset's project — same authorization intent as attaching — and audited. The
  Asset Detail "Photos & attachments" tiles get a remove button with a
  confirm step.
- CI status badge in `README.md`, linking to the GitHub Actions `ci.yml` workflow.

### Fixed

- **"Fetch from asset" never offered a photographed invoice.**
  `GET /api/v1/assets/{id}/expense-prefill` filtered candidates to
  `kind="doc"`, but the most common way an invoice reaches an asset is a phone
  photo of the paper one (`kind="photo"`) — so the endpoint returned an empty
  list for exactly the case it exists to serve. It now offers **every**
  attachment, ranked by `doc_type` (invoice → receipt → purchase order →
  quote, then everything else, newest first within each band).
- **The invoice never actually landed on the expense.** Copying an asset
  document required a separate manual click that only appeared *after* the
  expense had been saved, so the normal "fill the form and save" flow silently
  dropped it. Fetching from an asset now pre-ticks the best-ranked financial
  document and copies every ticked document automatically once the expense is
  created (a copy failure surfaces as a banner — the expense itself is already
  saved either way).
- `GET /api/v1/me` re-derives the effective permission set in Python (a
  deliberate no-N+1 optimization) and therefore had to learn about the new
  per-user overrides — otherwise it would advertise a permission set differing
  from what the server enforces, and every UI gate built on it would be wrong
  for exactly the users an admin adjusted.

## [0.14.0] - 2026-08-02

### Added

- **Edit your own name** — the Account screen now has a Profile card that sets
  your display name (`PATCH /api/v1/me`, audited as `user.profile_update`;
  `name` is the only self-settable field). The Dashboard greeting, sidebar and
  mobile "More" sheet show that name instead of your email address — `/me` now
  returns both the raw `name` (what the form edits, may be empty) and
  `display_name` (what the UI renders, falling back to the email until a name
  is set).
- **Lab identity in the app chrome** — the tenant's name is shown in the
  desktop sidebar's brand block, under the page title in the top bar, and in
  the mobile top bar, so it's clear which lab you're signed into on every
  screen.
- **Tenant logo, uploadable from the UI** — new Admin → **Lab Branding** screen
  (`/admin/branding`, `tenant.manage`) uploads, replaces, or removes the lab's
  logo, which then renders next to the lab name in the sidebar and mobile top
  bar (initials shown as a fallback). Backed by
  `GET/POST/DELETE /api/v1/tenancy/logo`: PNG/JPEG/WebP only (SVG deliberately
  rejected — stored-XSS vector), 2 MB cap, bytes on the media volume with only
  the storage key in the database, both mutations audited, and replacing a
  logo deletes the previous file.

### Fixed

- **`migrate` from an empty database works again after any `tenancy` schema
  change** — three seed data migrations (`rbac.0002`, `rbac.0003`,
  `projects.0006`) iterate the *concrete* `Tenant` model, so they selected
  columns that a later `tenancy` migration had not created yet and crashed a
  from-scratch `migrate` the moment the branding columns were added. They now
  select only the primary key.

## [0.13.0] - 2026-08-02

### Added

- **Public Docker Hub images + pull-based deploy** — CI builds and publishes
  both application images to Docker Hub: `mzahana/cortex` (the shared
  web/worker/beat/migrate image) and `mzahana/cortex-nginx` (nginx plus the
  built PWA). `docker-compose.prod.yml` now points at those tags instead of
  building locally, so deploying to the Synology is `pull && up -d` with no
  source checkout, no Dockerfiles, and no build on the NAS —
  `CORTEX_IMAGE_TAG` (default `latest`) selects the version. Local development
  with `docker-compose.yml` alone is unchanged and still builds from source.
- **Automated, drift-proof release versioning** — pushing a `vX.Y.Z` tag is now
  the single action that cuts a release: CI verifies the tag,
  `frontend/package.json`, and the CHANGELOG section all agree (failing the
  release if not), publishes `X.Y.Z` / `X.Y` / `latest` image tags, and creates
  the GitHub Release with notes taken verbatim from this file. `latest` tracks
  the newest *release*; `main` builds publish `edge` instead, so the
  prod-overlay default can never deploy an unreleased commit.

### Fixed

- **Backend CI gates are green again** — lint, typecheck and test had all been
  failing on `main`. The lint/typecheck failures were accumulated formatting
  and type-annotation drift (no runtime defects). The test failures were a real
  portability bug: `MEDIA_ROOT` defaults to the deployed image's `/app/media`
  volume path, so any test writing through `default_storage` passed inside a
  container but raised `PermissionError` on a bare CI runner; test settings now
  point it at a temp dir centrally.

## [0.12.0] - 2026-07-30

### Added

- **Per-tenant session idle/absolute timeout, admin-configurable** — sessions
  no longer live for a flat 7 days regardless of activity. A new
  `SessionSettings` table (one row per tenant, RLS-protected) holds
  `idle_timeout_minutes` (5–480, default 60) and `absolute_timeout_hours`
  (1–168, default 24); `SessionTimeoutMiddleware` checks both on every
  request and force-expires (session flush + `401 session-expired`
  problem+json) a session that's gone idle or outlived its absolute cap,
  regardless of activity. Tenant admins adjust both values from a new
  Admin → Session Settings screen (`GET/PATCH /api/v1/tenancy/session-settings`,
  gated on `tenant.manage`, audited as `session_settings.update` on every
  change) instead of an env var. The per-tenant bounds lookup is cached
  (60s TTL, invalidated on save) to add at most one extra query per tenant
  per minute rather than per request. Known, accepted gap: Django-admin
  (`/django-admin/`) superuser sessions aren't covered by this feature (they
  never carry the app's tenant session key this middleware keys off of) and
  still rely solely on the flat `SESSION_COOKIE_AGE` (kept at 7 days,
  specifically so it can't silently outlive this feature's own 7-day ceiling)
  — see `docs/risks.md` §3.

## [0.11.0] - 2026-07-28

### Added

- **README screenshots** — added a "Screenshots" section to `README.md` showing
  the login screen and dashboard, images under `docs/images/`.
- **Reservations: List view** — a new "List" tab alongside the existing
  Calendar on the Reservations screen (`ReservationsListPanel`), giving a
  filterable (status, asset), sortable, server-side-paginated table/list
  alternative for finding one specific reservation to act on, rather than
  clicking through the month/week/day agenda. Same `GET /api/v1/reservations`
  endpoint and the same `ReservationListItem` rows (approve/reject/cancel/
  checkout/checkin, permission-gated) as the Calendar view — no new backend
  endpoint, no client-side "load all reservations".
- **Email: built-in transactional content, no Brevo dashboard templates** —
  `BrevoProvider` now builds the subject/HTML/text for every transactional
  email (password reset, overdue reminder, low-stock alert, reservation
  confirmed, approval request, approval decision) locally
  (`apps.notifications.content`) and sends it via Brevo's inline-content API,
  instead of requiring a numeric Brevo dashboard template id per notification
  type. A non-technical admin now only needs an API key + verified sender to
  get real transactional email sending working. Added `BREVO_REPLY_TO` env
  var; `.env.example` and `docs/deployment-runbook.md` §2 rewritten for the
  new setup (env-only or fully-via-UI paths).
- **Email: "Send test email" on Admin → Email Settings** — new
  `POST /api/v1/notifications/email-settings/test` (`tenant.manage`, tightly
  throttled at `5/min` since it sends synchronously on the request thread)
  lets a tenant admin verify their saved Brevo API key/sender immediately
  after saving, instead of waiting for a real domain event. Always sends to
  the caller's own email using the tenant's already-saved settings; every
  attempt (success or failure) is recorded in `EmailLog` (`event_type=
  "test_email"`).
- **Reservations: auto-expire stale bookings** — new hourly Celery beat task
  `apps.reservations.expire_stale_reservations` sweeps every `pending`/
  `approved` reservation whose window has fully elapsed to `expired`,
  dropping it out of `Reservation.ACTIVE_STATUSES` so the window it held up
  is freed for re-booking without a manual cancel.

### Fixed

- **Reservation-backed checkout didn't enforce the reservation's time window
  (code-review finding)** — `POST /checkouts` with a `reservation` id now
  requires `now` to be inside that reservation's `[start_at, end_at)` window;
  checking out ahead of `start_at` or after `end_at` is rejected with a `400`
  (no grace period past `end_at` yet — documented default, `docs/risks.md`
  §3). A new terminal `Reservation.Status.COMPLETED` (set on checkin,
  `fulfilled` → `completed`) lets a user reuse their own already-approved
  window for a second checkout after an early return, without needing
  re-approval, still bounded by the original `end_at`.
- **A `fulfilled` reservation whose checkout was already checked in could
  block its window forever** — checking a reservation-backed checkout back in
  now moves the reservation `fulfilled` → `completed`, dropping it out of the
  no-overlap exclusion constraint so the window is free to rebook. A backfill
  migration (`0004`) applies the same transition retroactively to any
  reservation whose linked checkout was already checked in before this
  release.
- **Walk-up checkout could silently defeat another user's active
  reservation (code-review finding, blocking)** — `POST /checkouts` without a
  `reservation` id is now rejected with a `400` if another user holds an
  active (`pending`/`approved`/`fulfilled`) reservation on that asset covering
  the current time; a caller's own reservation never blocks their own
  walk-up, and a `reservation.approve` holder in that asset's scope may
  bypass this specific check.
- **Reservation approve/reject/cancel race conditions (code-review
  findings)** — `approve_reservation`, `reject_reservation`, and
  `cancel_reservation` now lock the reservation row and re-assert its status
  under that lock before writing a terminal transition, closing a window
  where a concurrent action (e.g. a checkout converting the reservation to
  `fulfilled`, or a competing cancel/reject) could be silently overwritten,
  potentially resurrecting a terminal reservation back into the no-overlap
  exclusion constraint or freeing a window the asset was still physically
  checked out under.

## [0.10.0] - 2026-07-27

### Added

- **Remove an expense's invoice/receipt attachment** — new
  `DELETE /api/v1/expense-attachments/{id}`, gated on `expense.manage` scoped
  to the expense's project, tenant-isolated (cross-tenant id `404`s), and
  audited (`expense_attachment`, before/after). Also purges the file off the
  storage backend on delete (best-effort — a missing/already-gone file is
  logged, not fatal), unlike the sibling `ProjectDocument` delete path, which
  still only removes the DB row. A trash-icon control on each attachment in
  the expense form calls it.
- **Project report: optionally embed invoice scans** — a checkbox on the
  Report tab ("Include invoice/receipt scans in the PDF") opts the generated
  project audit report into embedding each expense's invoice scan inline
  (default off). Image scans (JPEG/PNG/WebP/HEIC/HEIF, via `pillow-heif`)
  embed directly; PDF scans have their first page rasterized (PyMuPDF); all
  scans are downscaled to a 1600px-longest-edge cap before embedding to keep
  the report a sane size. DOCX/XLS/TXT invoices and any unreadable file fall
  back to the existing filename-only appendix listing rather than failing the
  report.
- **Project report: optionally append full project documents** — a second
  checkbox ("Include project documents ... in the PDF") appends the complete,
  original pages of each uploaded project document (proposals, contracts,
  progress reports, other) onto the end of the report, each preceded by a
  divider page naming the document. PDF documents are merged page-for-page;
  image documents are converted to a page (downscaled the same way as invoice
  scans); unsupported formats are skipped from the page-append (still listed
  in the appendix table) rather than failing the report. Bounded by a
  per-document page cap and a total document-count/aggregate-byte budget
  (checked against already-recorded upload size before any storage read) to
  keep worker memory bounded on a project with many/large documents.

### Fixed

- **Expense invoice/receipt upload silently rejected scanned images** — the
  expense form's file picker already accepted images, but always tagged the
  upload as `kind="doc"`, which the backend's content-type allowlist for that
  kind doesn't include (`doc` is PDF/Word/Excel/text only). Uploads now tag
  `kind="photo"` for `image/*` files, matching the backend's existing
  photo-content-type allowlist.

## [0.9.1] - 2026-07-26

### Added

- **Open-source license** — Cortex is now licensed under Apache License 2.0
  (`LICENSE`, `NOTICE`). Copyright and license are surfaced in the UI (login
  screen, desktop sidebar, mobile "More" sheet) and in the README.

## [0.9.0] - 2026-07-26

### Added

- **Password management** — the app previously had no way to change a password
  in the UI (the only password ever set was the generated one at user
  creation). Adds three flows:
  - **Self-service change** — `POST /api/v1/me/password` + an **Account**
    screen: a signed-in user enters their current password and a new one
    (validated against `AUTH_PASSWORD_VALIDATORS`); the session stays valid.
    Audited `user.password_change`.
  - **Admin reset** — `POST /api/v1/users/{id}/reset-password` + a "Reset
    password" action on the Users & Roles screen: an Admin (tenant-wide
    `user.manage`) regenerates a one-time password, revealed once (reusing the
    existing one-time-reveal modal). Cross-tenant id `404`s. Audited
    `user.password_reset`.
  - **Forgot password** — `POST /api/v1/auth/password-reset/request` +
    `/confirm` (both unauthenticated) with "Forgot password?" on Login and a
    reset-link landing screen. New tenant-owned `PasswordResetToken` table
    (RLS, single-use, TTL-bounded; only the SHA-256 hash is stored). The
    request endpoint is enumeration-safe (always a generic 200, dummy hash cost
    on miss) and emails the link via the `EmailProvider`. Audited
    `user.password_reset_request` / `user.password_reset_confirm`.

- **Project & grant management (M7)** — turns the thin `Project` config into a
  full grant hub without shifting the app away from its asset-first focus (a
  project stays an optional lens; `Asset.project_id` NULL = general pool,
  unchanged). See `docs/tasks/M7-project-grants.md`.
  - **Grant metadata + budget** on `Project`: `code`, `funding_source`
    (internal/external), `sponsor`, `start/end_date`, `budget_total`,
    `currency`, `status`, `description`. Budget `spent`/`remaining` and a
    per-category spend breakdown are **computed** (single aggregated query),
    never stored.
  - **Itemized expense/invoice ledger** — new `Expense` (project-scoped:
    amount, date, category, vendor, invoice number, optional link to the asset
    a purchase bought) with an `ExpenseAttachment` for the invoice scan; new
    seeded `ExpenseCategory` reference data (Equipment, Consumables, Services,
    Software, Travel, Shipping, Other), seeded per tenant via data migration +
    a `Tenant` post-save signal.
  - **Project documents** — `ProjectDocument` stores proposal/contract/
    progress-report files per project (binary on the storage backend, only
    `storage_key` + metadata in the DB, reusing the shared attachment writer).
  - **Audit-report PDF** — `POST /api/v1/projects/{id}/report` renders a
    structured report (budget summary, spend-by-category, itemized expenses,
    asset inventory with photo thumbnails, document appendix) in **Celery via
    WeasyPrint**, reusing the label/`jobs` async pipeline; plus a
    field-selectable streamed CSV export (`/projects/{id}/export.csv`).
  - **New endpoints** under `/api/v1`: richer `projects/{id}` detail+PATCH with
    budget rollup, `projects/{id}/assets`, `projects/{id}/expenses` +
    `expenses/{id}` + `expenses/{id}/attachment`, `projects/{id}/documents` +
    `documents/{id}`, `expense-categories`, `projects/{id}/report`, and
    `projects/{id}/export.csv`.
  - **New RBAC keys** `project.view` / `project.manage` / `expense.view` /
    `expense.manage`, enforced per-project (🟡): a Project Lead can only see and
    manage their own project's budget, expenses, and documents; Admins are
    tenant-wide. **Financial data is gated** — `budget_total`, spend, the
    expense/invoice ledger, and project documents require project-scoped
    `expense.view`; the project detail endpoint **redacts** (nulls) financial
    fields for non-privileged callers rather than 403-ing, so the non-financial
    project view still renders. Project create/delete stay Admin-only; a delete
    cascades the whole financial ledger and writes a single before/after audit
    snapshot of everything destroyed.
  - **Frontend** — new top-level **Projects** destination and a project hub
    (`/projects/:id`) with Overview/Budget, Assets (reuses the asset list),
    Expenses (ledger + form with invoice upload), Documents, and Report/Export
    tabs. Redacted financials render as an explicit locked state, never `$0`.
    The old thin `/admin/projects` CRUD is superseded by the hub.
  - Every new tenant table carries a fail-closed RLS policy and composite
    `(tenant, project)` indexes; RLS firing as the real `cortex_app` role is
    proven by test.

### Changed

- **Frontend PWA now builds inside Docker** — `nginx`'s image is now built
  from a new `docker/nginx/Dockerfile` (multi-stage: a `node:20-alpine`
  stage runs `npm ci && npm run build` against `frontend/`, then the built
  `dist/` is copied into the final pinned `nginx:1.27-alpine` stage). Removes
  the `./frontend/dist:/usr/share/nginx/html:ro` bind mount and the manual
  `npm install && npm run build` step it required before `docker compose up`
  — a plain Container Manager "Build" action (or `docker compose build`) now
  produces a working frontend with no NAS terminal/CLI step. `nginx.conf`/
  `default.conf` stay bind-mounted read-only for operator tweaking.

## [0.8.0] - 2026-07-25

Post-MVP feature batch: per-tenant email settings, reservation-first
checkout with a month calendar, consumable stock setup from the Asset
form, a "url" custom field type, My Items history, document attachments,
and several UX gap-fills.

### Added

- **Per-tenant email settings** (`apps/notifications`) — tenant admins can
  configure email provider/sender/Brevo API key from the UI
  (`GET/PUT/PATCH /api/v1/notifications/email-settings`, gated on
  `tenant.manage`) instead of only via env vars. The Brevo API key is
  encrypted at rest with `Fernet` (`apps.notifications.crypto`, keyed by
  the new `EMAIL_SETTINGS_ENCRYPTION_KEY` env var) and never round-trips in
  API responses. New "Email Settings" admin screen.
- **Reservation-first checkout + Asset Detail month calendar** — an
  approved reservation can be checked out/checked in directly from
  `ReservationListItem`; Asset Detail gets a "Reservations" section with a
  month-grid calendar view alongside the existing list. `GET /checkouts`
  gained `?asset=`/`?reservation=` filters and `ReservationSerializer.user`
  is now a nested `{id, email, name}` object.
- **Consumable stock setup from the Asset form** — `POST /api/v1/stock`
  (gated on `stock.adjust`; no dedicated "create stock item" RBAC key
  exists, documented as an assumption in `docs/rbac.md`) lets a consumable
  asset get its `StockItem` ledger row (unit/initial quantity/reorder
  config) at create time or via a standalone "Set up stock tracking" action
  on an existing asset. `GET /stock` also gained `?asset=`/
  `?include_retired=true` filters.
- **`url` custom field data type** — a new option alongside
  text/int/float/bool/date/enum/json, validated server-side with Django's
  `URLValidator` (http/https only).
- **My Items history tab** — a Current/History segmented control; History
  is `GET /checkouts?open=false`, most-recently-returned-first.
- **Document attachment uploads** on Asset Detail — `PhotoCapture` now also
  accepts non-image documents (receipts, purchase orders, etc.) via a
  second picker.
- Frontend component-test tooling (Vitest + Testing Library).

### Fixed

- Add Member's user picker now lists current users by default instead of
  staying empty until a search term is typed.

### Changed

- README rewritten as a numbered, step-by-step fresh-install guide.

## [0.7.0] - 2026-07-21

Milestone **M6 — Import/export + deploy hardening**: bulk CSV/Excel import
with dry-run validation, filtered CSV export, production deploy hardening,
a real Cloudflare Tunnel operator runbook, a proven backup/restore drill,
and a 50k-asset load test. Meets F11 acceptance; F1-F11 confirmed proven
(not just claimed) by the final MVP-wide gate. **End of MVP.**

### Added

- **Bulk importer** (`apps/imports`) — `POST /api/v1/imports` (upload CSV or
  `.xlsx`, dry-run column-mapping + a per-row validation report) →
  `POST /api/v1/imports/{id}/commit` (all-or-nothing asset creation
  including custom-field values, reusing the existing validation path),
  polled via the generic `apps/jobs` (`GET /api/v1/jobs/{id}`, introduced in
  M4). No representative spreadsheet sample was available (Q8 unanswered in
  `docs/risks.md`) — the column schema (name/category/location/project/
  tags/status/condition + per-category custom fields) is a documented
  assumption. `GET /api/v1/exports/assets.csv` streams the same
  filtered/RBAC-scoped queryset the asset list uses; import↔export
  round-trips against the same schema.
- **Import wizard & Export CSV** — upload → mapping review with a per-row
  error table → commit (blocked while any row is invalid) → success
  summary; the Asset List's live filter state now drives an Export CSV
  link.
- **Production hardening** — `docker-compose.prod.yml` overlay
  (`DEBUG=false`, tunable gunicorn/Celery concurrency); most of `docs/
  deployment.md` §7's checklist (pinned image digests, mem budgets, nginx
  security headers, non-superuser RLS-subject role) was already correct
  from earlier milestones and is now verified item-by-item.
- **Deploy runbook** (`docs/deployment-runbook.md`) — a numbered,
  copy-pasteable operator guide for the Cloudflare Tunnel/DNS/SSL setup,
  SPF/DKIM/DMARC record templates, and Synology Container Manager bring-up,
  using a placeholder hostname/domain pending Q3 (unanswered in `docs/
  risks.md`).
- **Backups & a proven restore drill** — `docker/backup/backup.sh` (nightly
  `pg_dump -Fc`, 7-daily + 4-weekly rotation, cron-invocable). The restore
  drill was actually executed end to end: seed → back up → destroy the
  stack entirely → restore into a brand-new stack → byte-for-byte (sha256)
  verification that a restored attachment and label PDF matched the
  originals, with login/RLS/tenant-scoping intact.
- **50k-asset load test** (`tests/load/`) — real session-auth/CSRF/RBAC/RLS
  HTTP load via Locust at 30 concurrent users against a prod-profile stack.
  3 of 5 `docs/architecture.md` §4 p95 targets met (detail 120ms, cached
  dashboard 87ms, checkout write 41ms). Asset list/search miss their
  targets (450ms/740ms vs 300ms/500ms) — root-caused to a genuine
  PostgreSQL/RLS interaction: the RLS tenant predicate is enforced as a
  security barrier, and neither the full-text-search (`@@`) nor trigram
  (`%`) operator is marked `LEAKPROOF`, so the planner cannot push them
  below the barrier to use the GIN/trgm indexes. The only fix that
  restores the fast plan (`ALTER FUNCTION ... LEAKPROOF`) would override a
  deliberate upstream PostgreSQL security decision, cluster-wide, for a
  performance win — explicitly **not applied**, by user decision, given
  this project's tenant-isolation-first invariant. Documented as an
  accepted gap, with a regression tripwire (`test_perf_rls_search_plan.py`,
  a strict `xfail`) so it can never silently regress further or silently
  resolve unnoticed.

### Fixed

- `GET /api/v1/exports/assets.csv`'s streaming response ran its
  `prefetch_related` after Django's tenant-context middleware had already
  unwound the request context, raising `TenantContextError` mid-stream —
  fixed by re-entering `tenant_context(...)` inside the generator body.
- Closed a structural blind spot in the M1 search perf test: its `EXPLAIN`
  assertions ran on the DB **owner** connection, which bypasses RLS
  entirely, so it could never have caught the list/search regression above.
  A new test drives the same query through the real RLS-subject role.

## [0.6.0] - 2026-07-21

Milestone **M4 — Mobile scan, photo, labels (MVP)**: installable PWA,
camera QR scan-to-asset, camera photo capture, and server-rendered Avery
label sheets. Meets F6 + F7 acceptance (automated coverage; on-device
verification over the Cloudflare Tunnel is still owed once M6's deploy
lands — see carry-forwards).

### Added

- **`GET /api/v1/resolve/{qr_token}`** — tenant-scoped scan resolver behind
  the same `asset.view` RBAC as Asset Detail; an unknown or cross-tenant
  token both 404 (never leaking existence). Under the 250ms perf budget.
- **Installable PWA** — a manifest plus a Workbox service worker
  (`vite-plugin-pwa`, `generateSW`) precaching only the app shell; `/api/**`
  is never intercepted, so no stale data is ever served offline.
- **Scan screen** — camera QR scan (`@zxing/browser`) resolves straight to
  Asset Detail with check-in/out offered; an always-visible manual
  token/asset-ID entry field covers camera-unavailable/denied (risk R5).
  Reachable from a dashboard FAB.
- **Camera photo capture** — `capture="environment"` file input uploads
  directly to the existing attachments endpoint with an optimistic
  in-progress tile; the photo renders on the asset within seconds, no page
  reload.
- **Label PDF generation** — a new generic `apps/jobs` (tenant-scoped async
  job polling, `GET /api/v1/jobs/{id}`) backs `POST /api/v1/labels/generate`
  (`label.generate`, scoped): a Celery task renders each selected asset's
  `qr_token` as a `segno` QR (explicitly non-Micro, so phone cameras can
  read it) laid out via WeasyPrint onto Avery 5160/5163 sheet templates
  (Q9's documented default — unanswered in `docs/risks.md`). The full
  generate → decode → scan round trip was proven end-to-end against a real
  running stack: the rendered PDF's QR codes were decoded back to their
  exact source tokens and resolved to the exact correct asset via the scan
  screen's manual entry.

### Fixed

- The QR-decode test dependency (`opencv-python-headless`) shipped no
  musllinux wheel, so it could never install inside the project's actual
  Alpine-based app image — only ever worked on CI's glibc runners. Replaced
  with `pymupdf` + the `zbarimg` CLI so `pip install -r requirements/dev.txt`
  works in the same image the app actually runs in.
- The service worker's navigation fallback would have intercepted `<a
  download>` clicks on generated PDFs (label sheets, attachments) once
  active in production, serving the cached app shell instead of the file
  — excluded `/media/` from the fallback denylist.

## [0.5.0] - 2026-07-20

Milestone **M5 — Notifications (Brevo) + audit + dashboard**: async email via
the provider interface, domain events from M2/M3 wired to templates,
throttled beat scans, a finalized audit trail with DB-level immutability, and
the live dashboard. Meets F8 + F9 + F10 acceptance.

### Added

- **`EmailProvider` abstraction** — `ConsoleProvider` (dev/test default) and
  `BrevoProvider` (transactional API, env-gated, unexercised pending Q6's
  sender-identity confirmation) behind a protocol business logic never
  imports directly. Every send goes through a Celery task with retry/
  backoff, logged to `EmailLog` (`queued`/`sent`/`failed`/`bounced`);
  `NotificationPref` gates optional events per user x event type.
  `GET/PATCH /api/v1/notification-prefs`.
- **Domain events wired to email** — `reservation_confirmed`,
  `approval_request` (union-of-memberships recipient resolution: tenant-wide
  Admin + the asset's project lead), `approval_decision`, and
  `low_stock_alert` all route through the enqueue pipeline. Overdue and
  low-stock reminders are hourly Celery beat scans, each throttled
  independently per item via a Redis guard.
- **Audit finalized** — added the `user.manage`/`role.assign` endpoint that
  was missing entirely (`POST/PATCH/DELETE /api/v1/memberships`: Admin
  tenant-wide; ProjectLead may add/remove members and assign only Member
  within their own project, never a co-lead or Admin). `GET /api/v1/audit`
  (Admin tenant-wide, ProjectLead scoped to their project's assets). A
  database-level append-only trigger now backs the app-layer guard —
  rejects any UPDATE/DELETE on `audit_log`, even from the table owner.
- **`GET /api/v1/dashboard/summary`** — six scope-aware tiles (totals by
  category, currently-out, overdue, low-stock, upcoming reservations,
  per-project allocation), Redis-cached (30s TTL + event-based invalidation
  on the highest-value mutations), proven under an 800ms budget at 10k-asset
  scale.
- **Dashboard/Home, My Notifications, and Audit Log screens** — the
  dashboard is now the post-login landing route; notifications lists all
  five event types with a per-event email toggle; the audit log is
  filterable, paginated, and read-only.



Milestone **M3 — Reservations & check-in/out**: durable-asset reservations
with DB-enforced conflict rejection, per-category approval, check-in/out with
overdue detection, and the calendar/My-Items/Approvals UI. Meets F4 + F5
acceptance.

### Added

- **Reservation & Checkout models** — a GiST exclusion constraint
  (`reservation_no_overlap_active`) rejects overlapping active reservations
  (pending/approved/fulfilled) per asset at the database level; RLS on both
  new tables; partial indexes for the open/overdue checkout scan.
- **Reservation endpoints** — create (conflict + configurable per-user cap,
  routed to `pending` or auto-`approved` per `Category.requires_approval`),
  approve/reject (scoped `reservation.approve`, general-pool assets
  Admin-only), cancel, and the calendar feed (`GET /reservations?from&to`). A
  DB-level conflict surfaces as a clean `409`, never a raw error. Every
  mutating action is audited; `reservation_confirmed`/`approval_request`/
  `approval_decision` domain events are emitted for M5.
- **Checkout endpoints** — check out (optionally from an approved
  reservation, which now transitions to `fulfilled` so its window keeps
  blocking new bookings while the asset is out), idempotent check-in with
  condition notes, scoped `checkout.override` force-return, and an
  open/overdue list filter backed by the partial indexes.
- **Reservations Calendar** — month/week/day view with live conflict
  feedback on create and in-place approve/reject for scoped approvers.
- **Approvals screen** — pending reservation and reorder-request approvals
  in the user's scope.
- **My Items screen** — the user's open checkouts with due dates, overdue
  highlighting (trusting the server's computed `is_overdue`), and one-tap
  check-in; Asset Detail's reserve/check-out/check-in actions are now wired
  to the real API.

## [0.3.0] - 2026-07-20

Milestone **M2 — Consumables & stock**: immutable ledger-backed quantity
tracking, low-stock detection, and the reorder workflow, with UI. Meets F3
acceptance.

### Added

- **Stock models** — `StockItem` (a 1:1 extension of a consumable Asset:
  unit of measure, quantity on hand, reorder threshold/target, bin location),
  an immutable `StockTxn` ledger (receive/consume/adjust/correction), and
  `ReorderRequest` with a validated status lifecycle
  (open → approved → ordered → received → cancelled).
- **Stock endpoints** — `GET /stock` (server-side paginated, low-stock
  filterable, scope-aware) and `POST /stock/{id}/txn`, which applies a ledger
  transaction and reconciles quantity atomically under a row lock; quantity is
  always derived from the ledger, never set directly, and a transaction that
  would go negative is rejected. Reorder-request create, approve, and status
  transitions, enforcing the project-scoped `stock.adjust`/`stock.consume`/
  `reorder.request`/`reorder.approve` permissions.
- **`low_stock` domain event** — emitted once on the threshold-crossing edge
  (idempotent, transactional) for M5's email notification to consume.
- **Stock / Consumables screen** — quantities with live updates and low-stock
  highlighting, receive/consume/adjust actions, and the reorder-request and
  approval flow, gated by the user's effective permissions.

### Security

- The ledger invariant — `quantity_on_hand` always equals the sum of the
  ledger — is enforced at the database level, independent of the application:
  a reconciliation trigger, a validating trigger that rejects any write that
  would desync the two, and a trigger that makes the ledger append-only
  (an update or delete is rejected; a correction is always a new row). Row-Level
  Security backs all three new tenant-owned tables, and a `StockItem` can only
  be created for a consumable asset, enforced at both the application and the
  database layer.
- Every `stock.adjust` transaction and reorder approval writes an immutable
  audit-log entry.

## [0.2.0] - 2026-07-19

Milestone **M1 — Asset registry core**: the heterogeneous asset registry with
custom fields, trees, full-text search, and the admin/list/detail/edit UI.
Meets F2 acceptance; a 10k-row corpus searches in well under 500 ms server-side.

### Added

- **Catalog** — `Category` and `Location` self-referential trees, typed
  `CustomFieldDef`s (text/int/float/bool/date/enum/json with unit, enum options,
  required, order), `Tag`s, and the `Project` model (extended with a lead), all
  tenant-scoped with server-side-paginated CRUD endpoints and
  `category.manage`/`location.manage` enforcement.
- **Assets** — the `Asset` model (public UUID + unguessable `qr_token`, category,
  status lifecycle, condition, holder, JSONB custom field values validated
  against the category's field defs), `Attachment`s (bytes on the media volume,
  only the key in the database, with a size cap and content-type allowlist),
  tag links, and CRUD + retire (hide-but-retain, audited) + attachment upload.
  Asset actions are RBAC-enforced with the project-scoped union-of-memberships
  rule.
- **Asset list & search** — server-side pagination (page and cursor), full-text
  search (weighted `tsvector` maintained by database triggers) with `pg_trgm`
  fuzzy fallback and relevance ranking, whitelisted ordering, and filters by
  category, status, location, project, tag, and consumable flag — scope-aware so
  a project-scoped user sees only their assets, with bounded query counts.
- **Audit log** — an append-only `AuditLog` (pulled forward to satisfy the audit
  invariant for `asset.retire`; database-level immutability lands in M5).
- **Admin & asset UI** — a reusable tree component; admin screens for the
  category/field-definition and location trees; a virtualized, filterable,
  searchable Asset List (card and table views); an Asset Detail screen rendering
  typed custom fields with permission-gated actions; and a category-driven
  dynamic Asset create/edit form.
- **Performance tooling** — a reusable 10k+ asset seed command and a perf test
  suite asserting sub-500 ms paginated list and search at scale.

### Changed

- Database constraint violations (duplicate names, protected deletes) now return
  RFC 7807 4xx responses instead of HTTP 500.
- Django's admin moved from `/admin/` to `/django-admin/` so the single-page
  app owns the `/admin/*` route namespace.

### Security

- Row-Level Security policies added to all nine new tenant-owned tables via the
  shared helper, keeping the application filter and the database backstop in
  lockstep; verified by a runtime test that drives a real request over the
  non-superuser application role.
- Tenant provisioning fixed to seed roles within the new tenant's context so it
  works under Row-Level Security. Attachment uploads reject executable/script
  content types; user-supplied content is escaped throughout the UI.

### Fixed

- Renaming a tag now refreshes the search vectors of every asset carrying it.

## [0.1.0] - 2026-07-19

Milestone **M0 — Foundations**: the stack boots, login works, and multi-tenant
isolation is enforced at two independent layers and proven by tests in CI.

### Added

- **Repository scaffold** — `backend/` (Django + DRF), `frontend/` (React + TS +
  Vite PWA), `docker/` (compose, nginx, cloudflared), design docs in `docs/`, and
  `.env.example` documenting every configuration key.
- **Container stack** — `docker-compose.yml` with all seven services
  (postgres 16, redis 7, web, worker, beat, nginx, cloudflared) under a
  ~3.25 GB memory budget, a shared application image, a one-off owner-role
  `migrate` step, capped Redis (`maxmemory 256mb allkeys-lru`), and nginx serving
  the built PWA behind the Cloudflare Tunnel with security headers.
- **Django project** — 12-factor split settings via `django-environ`, Redis as
  cache + session store + Celery broker/result backend, Celery + beat wiring, and
  the versioned `/api/v1` API surface with RFC 7807 error responses.
- **Multi-tenant core & RBAC** — `Tenant`, custom `User` (email unique per
  tenant), `Role`, `Permission`, `RolePermission`, and `Membership` models; the
  central fail-closed tenant-scoped base manager; the union-of-memberships
  effective-permission resolver (tenant-wide vs. project-scoped); and an
  idempotent seed of the four system roles and full permission-key set.
- **Session authentication** — `POST /api/v1/auth/login`, `POST /api/v1/auth/logout`,
  `GET /api/v1/me` (user, memberships, effective permissions), plus
  `GET /api/v1/auth/csrf`. Redis-backed sessions, Secure/HttpOnly/SameSite
  cookies, CSRF on writes, per-IP request throttling, and failure-based login
  lockout. The auth contract is frozen in `docs/api-and-ui.md`.
- **Frontend** — Vite + React + TypeScript + Mantine scaffold with routing, a
  typed API client (CSRF handling, RFC 7807 parsing), a Login screen, and an
  authenticated shell that reads `GET /me`.
- **Continuous integration** — GitHub Actions pipeline: ruff/black, mypy,
  ESLint, `tsc`, Vite build, pytest against a throwaway Postgres 16, migrations
  applied **up and down**, `manage.py check --deploy` under production settings,
  and an application-image build.
- **Test suite** — multi-tenant `factory_boy` factories defaulting to distinct
  tenants; tenant-isolation, RBAC-scope, and query-budget tests; and a canonical
  Row-Level Security test verified with a negative control.

### Security

- **Tenant isolation is enforced centrally and defence-in-depth.** The tenant is
  derived only from the server-side session, never from client input. Every
  tenant-owned query passes through the fail-closed tenant-scoped manager, and
  **PostgreSQL Row-Level Security** is the backstop: the runtime connects as a
  dedicated non-superuser, `NOBYPASSRLS` role (`cortex_app`) so policies actually
  fire, while migrations run as the owner. A missing tenant context yields zero
  rows rather than leaking across tenants.
- Argon2 password hashing; production `SECURE_*` headers, HSTS, and secure
  cookies configured from the environment; no secrets committed to the repository
  or baked into images.
