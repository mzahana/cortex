# Features — MVP vs. Phases

Phasing is designed so **MVP is deployable and genuinely useful**, then extended.
Acceptance criteria are given for MVP items.

## MVP (Phase 1) — "track, scan, reserve, alert"

### F1. Auth, tenants, RBAC
- Email/password login (Argon2), Redis-backed sessions, secure cookies.
- Multi-tenant isolation; roles Admin/ProjectLead/Member/Viewer; project-scoped memberships.
- **Acceptance:** A user only ever sees their tenant's data; a Member cannot hit an admin endpoint (403 server-side even if URL is guessed); a ProjectLead can approve only their project's requests.

### F2. Asset registry with custom fields
- CRUD assets; category tree; per-category custom fields (JSONB-backed); location tree; status lifecycle; purchase/warranty/serial; tags; photos.
- Consumable vs. durable flag; general-pool vs. project assignment.
- **Acceptance:** Admin can create a Compute asset with GPU/VRAM custom fields and a Component consumable with quantity; assets list filterable by category/status/location/project/tag; a retired asset is hidden from default lists but retained.

### F3. Stock & consumables
- Quantity + unit of measure; immutable stock ledger; low-stock threshold; restock/receive; reorder request (open→received).
- **Acceptance:** Consuming stock decrements quantity via a ledger txn; crossing the reorder threshold flags low-stock and (F9) emails; quantity always reconciles to the ledger sum.

### F4. Reservations & calendar
- Reserve a durable asset for a time window; **conflict detection** (no overlapping active reservations); calendar view; **per-category approval** (auto vs. pending→approved); configurable per-user reservation limit.
- **Acceptance:** Two overlapping reservations on one asset are rejected at the DB level; an approval-required category creates a `pending` reservation that a scoped approver can approve/reject; approved windows show on the calendar.

### F5. Check-in / check-out
- Check out (optionally from a reservation) with due date; check in with condition notes; open-items view; **overdue detection**.
- **Acceptance:** Checking out sets asset `in_use` and creates an open checkout; checking in records condition and frees the asset; an item past `due_at` is flagged overdue and appears on the dashboard.

### F6. Mobile PWA + QR scanning + photo capture
- Installable PWA; **camera QR scan** opens the exact asset; **camera photo capture** attaches to the record. Requires the HTTPS/secure-context deployment (see `deployment.md`).
- **Acceptance:** On a phone over the Cloudflare HTTPS domain, scanning an asset's QR opens its detail page and offers check-in/out; a captured photo appears on the asset within seconds (upload via background where possible).

### F7. PDF label generation
- Generate a label (QR encoding the asset's stable token/URL + human-readable name, ID, category, location); single or **batch**; laid out on an **Avery-style sheet PDF**; sizes selectable.
- **Acceptance:** Admin selects N assets → gets a print-ready PDF whose QR codes, when scanned by F6, resolve to those exact assets.

### F8. Audit trail
- Immutable log of movements, stock changes, reservations, and admin actions.
- **Acceptance:** Every check-out, stock adjust, reservation approval, and role change produces a tamper-evident audit entry with actor/time/before-after; entries cannot be edited or deleted via the app.

### F9. Email notifications via Brevo (async)
- Provider interface + Brevo API impl; Celery-sent; templated; `EmailLog`; per-user prefs. Events: reservation confirmation, approval request/decision, overdue reminder, low-stock alert.
- **Acceptance:** Triggering an event enqueues (never blocks the request); a failed send retries and is logged as `failed`; a user who disabled an optional event gets none; SPF/DKIM/DMARC configured so mail lands.

### F10. Dashboard, search, filter
- Server-side search (full-text + fuzzy) and filters; dashboard: totals by category, currently-out, overdue, low-stock, upcoming reservations, per-project allocation.
- **Acceptance:** Search/list over 10k+ seeded assets returns < 500 ms server-side and is paginated; dashboard tiles reflect live state.

### F11. Bulk import/export
- CSV/Excel import with column mapping + validation preview; CSV export of filtered lists. Runs as a background job for large files.
- **Acceptance:** A messy spreadsheet imports via a mapping step with a dry-run error report; valid rows create assets (incl. custom fields); export round-trips.

## Phase 2 — "operate & maintain"
- Maintenance & calibration scheduling with due/overdue flags and reminders (data model is in MVP; scheduling UI + Beat scans here).
- Issue reporting workflow (report → triage → resolve) with status.
- Richer reporting: utilization, inventory value, consumption trends, per-project usage; scheduled report exports.
- Notification preferences UI polish; digest emails.
- Saved searches / advanced filter builder.
- Reservation recurring windows; check-out via QR kiosk mode.

## Phase 3 — "scale & integrate"
- SSO/OAuth (Google/Microsoft) + optional Cloudflare Access rollout.
- Dedicated label-printer support (Brother/Dymo) alongside PDF sheets.
- S3-compatible object storage backend; move DB to managed Postgres (tier-2).
- Barcode (1-D) support; asset relationships (kits ↔ components BOM).
- Webhooks/API tokens for external integrations; multi-tenant self-service onboarding.
- Offline-first PWA queueing of scans/checkouts.

## Explicitly deferred / anti-gold-plating
- Native mobile apps (PWA suffices).
- Procurement/PO/finance and depreciation accounting.
- Real-time device telemetry.
- Per-tenant custom domains / white-labeling.
- A separate search engine (Postgres FTS is enough at this scale).
