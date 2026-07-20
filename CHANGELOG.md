# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Versions map to milestones in `docs/tasks/`: **M0** → `0.1.0`, and subsequent
milestones bump the minor version until the first production release (`1.0.0`).

## [Unreleased]

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
