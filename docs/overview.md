# LMS — Lab Asset & Inventory Management Platform

## 1. Purpose

A web-based, mobile-first platform to track every physical asset in a robotics
research lab: what we own, where it is, who has it, when it's due back, and when
consumables run low. It replaces the current "no system / lost track" situation
with a single source of truth that is fast, auditable, and usable from a phone
in the workshop.

## 2. Goals

- **One source of truth** for a heterogeneous inventory (PCs/GPUs, edge devices,
  drones and drone electronics, general components, workshop tools, instruments).
- **Frictionless day-to-day use**: scan a QR label with a phone camera →
  check out / check in / view in seconds.
- **Never run dry**: consumables tracked by quantity with low-stock alerts and a
  lightweight reorder request flow.
- **Accountability**: reservations, check-in/out, and an immutable audit trail of
  who did what and when.
- **Self-hostable and cheap**: runs on a Synology DS220+ via Docker, reachable
  securely from anywhere over a Cloudflare Tunnel, with a clean path to move to
  the cloud later without a rewrite.

## 3. Scope (confirmed with stakeholder)

| Decision | Choice |
|---|---|
| Peak concurrent users | ~10–30 (up to ~300 registered) |
| Tenancy | **Multi-tenant from day one** (multiple independent labs/orgs, isolated data) |
| Growth horizon | ~50k asset records / ~300 users over 2–3 years |
| Edge auth | App login only; Cloudflare Access documented as optional toggle |
| Labels | **PDF label sheets** (Avery-style) via office printer; label-printer support deferred |
| Approvals | **Configurable per category** (auto-approve vs. require approval) |
| Import | Excel/CSV importer **and** manual entry |
| Stack | Recommended: Django + DRF + PostgreSQL + React/TypeScript PWA |
| Deployment | `docker-compose` on Synology Container Manager + Cloudflare Tunnel |

### In scope (see `features.md` for MVP vs. later)
Asset registry with category-specific custom fields; consumable stock &
reorder; reservations with calendar + conflict detection + configurable
approval; check-in/out with overdue detection; mobile QR scanning + photo
capture (PWA); PDF label generation; maintenance/calibration tracking; audit
trail; dashboards; search/filter/tags; CSV/Excel import-export; Brevo
transactional email behind a provider interface; basic reporting; RBAC with
project- and tenant-scoping.

### Out of scope (initial)
Native mobile apps (PWA instead); procurement/purchase-order/finance
integration; barcode label-*printer* hardware integration (PDF sheets only for
MVP); SSO/OAuth (designed-for, not built in MVP); accounting/asset-depreciation
beyond simple purchase-value reporting; IoT/live telemetry from devices.

## 4. Key assumptions

1. **6 GB RAM on the NAS.** The prompt body states the DS220+ has 6 GB (its max);
   deliverable #7 said "2 GB". We design to 6 GB and verify the footprint also
   fits a tighter budget. (Flagged in `risks.md`.)
2. **Multi-tenant = data isolation, not full white-label.** Each tenant (lab/org)
   has its own users, assets, projects, and admins; a `tenant_id` scopes every
   row and every query. No custom domains per tenant in MVP.
3. **Photos/attachments live on a mounted volume** (object-storage-compatible via
   `django-storages`), never as DB blobs.
4. **DNS is on Cloudflare**, so Tunnel + SPF/DKIM/DMARC records are easy to add.
5. **Brevo** is the email provider, reached via its transactional **API** (see
   `architecture.md` for API-vs-SMTP rationale), abstracted behind an interface.
6. **QR codes** (not 1-D barcodes) are the default label symbology — denser, easy
   to scan from a phone, and encode a stable per-asset URL.
7. Users are trusted lab members; the threat model is opportunistic internet
   exposure, not a hardened public SaaS. Hardening steps are still applied.

## 5. Success criteria (MVP)

- A member can, from their phone, scan an asset and check it out in < 15 seconds.
- Admin can bulk-import the existing inventory from a spreadsheet.
- Low-stock and overdue events generate Brevo emails without blocking any request.
- List/search over 10k+ assets returns in < 500 ms server-side (see
  `architecture.md` performance targets).
- The whole stack runs within the DS220+ 6 GB budget with headroom.
- Moving the DB to managed Postgres or the app to a cloud VM is a config change,
  not a code change.
