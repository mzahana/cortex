# Implementation Roadmap

Sequenced so each milestone is deployable and testable. Rough effort is relative
(S/M/L), not calendar-committed. Dependencies noted.

## Milestone 0 — Foundations (scaffold)
- Repo layout: `backend/` (Django+DRF), `frontend/` (React+TS+Vite PWA),
  `docker/` (compose, nginx, cloudflared), `docs/`.
- `docker-compose.yml` with all 7 services; local dev via compose.
- Base Django project: settings via env (12-factor), Postgres, Redis, Celery wired.
- CI: lint, type-check, test, build images.
- **Tenant + User + Role + Permission + Membership** models and migrations; RLS
  policies; auth + `/me`.
- **Dep:** none. **Effort:** M. **Exit:** login works, tenant isolation enforced in a test.

## Milestone 1 — Asset registry core (MVP)
- Category tree + custom field defs; Location tree; Project; Tag.
- Asset CRUD + custom field values + attachments (volume storage) + status
  lifecycle; FTS `search_vector` + indexes.
- Asset List (paginated, server-side search/filter) + Asset Detail/Edit UI.
- **Dep:** M0. **Effort:** L. **Exit:** F2 acceptance met; 10k-row seed searches < 500 ms.

## Milestone 2 — Consumables & stock (MVP)
- StockItem + StockTxn ledger + reorder requests; low-stock partial index.
- Stock UI (receive/consume/reorder).
- **Dep:** M1. **Effort:** M. **Exit:** F3 acceptance met.

## Milestone 3 — Reservations & check-in/out (MVP)
- Reservation with GiST overlap exclusion + per-category approval + limits;
  Checkout/checkin; overdue flag.
- Calendar UI, My Items, Approvals UI.
- **Dep:** M1. **Effort:** L. **Exit:** F4 + F5 acceptance met (overlap rejected at DB).

## Milestone 4 — Mobile scan, photo, labels (MVP)
- PWA manifest + service worker (app shell); camera QR scan → `/resolve/{token}`;
  camera photo capture → attachment.
- Label PDF generation (segno QR + WeasyPrint Avery sheets) via Celery.
- **Dep:** M1 (+ HTTPS deploy for real-device test). **Effort:** M. **Exit:** F6 + F7 acceptance met on a phone over the Tunnel domain.

## Milestone 5 — Notifications (Brevo) + audit + dashboard (MVP)
- EmailProvider interface + BrevoProvider + Celery async + EmailLog + prefs.
- Beat scans: overdue reminders, low-stock alerts.
- AuditLog writes across all mutating actions (some added earlier; finalize here).
- Dashboard summary (cached) + tiles.
- **Dep:** M2, M3. **Effort:** M. **Exit:** F8 + F9 + F10 acceptance met.

## Milestone 6 — Import/export + deploy hardening (MVP complete)
- CSV/Excel importer (mapping + dry-run + commit, background); filtered CSV export.
- Deploy to DS220+ via Container Manager; Cloudflare Tunnel + DNS + SPF/DKIM/DMARC;
  backup jobs; hardening checklist; load test to verify perf targets.
- **Dep:** M0–M5. **Effort:** M. **Exit:** F11 met; live on `cortex.yourdomain.com`; backups tested; load test passes.

> **End of MVP.** Deployable, seeded from your spreadsheet, usable in the workshop.

## Phase 2 — Operate & maintain
- Maintenance/calibration scheduling UI + due/overdue reminders (Beat) — data model already present.
- Issue reporting workflow.
- Reporting suite (utilization, inventory value, consumption, per-project) + scheduled exports.
- Notification digests + preference UI polish; saved searches.
- **Dep:** MVP. **Effort:** L.

## Phase 3 — Scale & integrate
- SSO/OAuth + optional Cloudflare Access rollout (admin routes).
- Tier-2: move DB to managed Postgres (config change) + object storage backend.
- Tier-3: app tier as N replicas on a cloud VM/container platform + PgBouncer.
- Label-printer (Brother/Dymo) support; 1-D barcodes; kit↔component BOM; API tokens/webhooks; offline scan queue.
- **Dep:** Phase 2 + load/growth triggers. **Effort:** L.

## Critical path & parallelization
- Critical path: **M0 → M1 → (M2 ∥ M3) → M5 → M6**.
- M4 (scan/labels) can proceed in parallel after M1.
- Frontend and backend can be built in parallel per milestone against the API
  contract in `api-and-ui.md`.
- Load testing (M6) should be dry-run against M1 early to catch index/query issues.
