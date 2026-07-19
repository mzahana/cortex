# M3 — Reservations & check-in/out (MVP)

**Goal:** durable-asset reservations with DB-level conflict rejection, per-category
approval, check-in/out with overdue detection, and the calendar/My-Items/Approvals UI.
Runs in parallel with M2 after M1.
**Dep:** M1. **Effort:** L.
**Milestone exit:** F4 + F5 acceptance met (overlap rejected at the DB).
Refs: `docs/data-model.md`, `docs/features.md` F4/F5, `docs/rbac.md` §4, `docs/api-and-ui.md`.

---

### T3.1 · → db-migration-specialist · deps: M1
**Do:** Reservation and Checkout tables + migrations. **GiST exclusion constraint**
rejecting overlapping active reservations per asset:
`EXCLUDE USING gist (asset_id WITH =, tstzrange(start_at,end_at) WITH &&)` limited to
active statuses (pending/approved/fulfilled). Partial index
`checkout (tenant_id, checked_in_at)` where `checked_in_at IS NULL` (open items). RLS on
both tables. (`btree_gist` already enabled in M0.)
**Files:** `backend/apps/reservations/migrations/`.
**Exit:** up/down clean; a direct SQL insert of an overlapping active reservation is
rejected by the constraint.

### T3.2 · → backend-engineer · deps: T3.1
**Do:** Reservation model + endpoints: `POST /reservations` (checks the exclusion
constraint conflict and the configurable **per-user reservation limit**; honors
`Category.requires_approval` — auto-approve vs. create `pending`), `approve`/`reject`
(scoped to `reservation.approve`; general-pool assets approved by Admins),
`cancel`, and the calendar feed `GET /reservations?from&to`. Every approve/reject writes
an AuditLog entry. Emit `reservation_confirmed` / `approval_request` / `approval_decision`
domain events (consumed by M5).
**Files:** `backend/apps/reservations/api.py`, `services.py`.
**Exit:** F4 acceptance: overlap rejected at DB (surfaced as a clean 409); approval-
required category yields `pending` that a scoped approver can approve/reject; approved
windows appear on the calendar feed.

### T3.3 · → backend-engineer · deps: T3.1
**Do:** Checkout endpoints: `POST /checkouts` (optionally from a reservation; sets asset
`in_use`, creates an open checkout with `due_at`), `POST /checkouts/{id}/checkin`
(records condition, frees the asset — idempotent), `POST /checkouts/{id}/override-return`
(`checkout.override`, scoped, audited), `GET /checkouts?open=true&overdue=true`. Overdue
= open past `due_at`.
**Files:** `backend/apps/reservations/checkout.py`.
**Exit:** F5 acceptance: checkout sets `in_use` + open checkout; checkin records condition
and frees the asset; a past-`due_at` item is flagged overdue.

### T3.4 · → frontend-engineer · deps: T3.2
**Do:** Reservations Calendar (month/week/day; create with live conflict feedback;
approve/reject in-place for approvers) and Approvals screen (pending reservation/reorder
approvals in the user's scope).
**Files:** `frontend/src/screens/reservations/*`, `frontend/src/screens/approvals/*`.
**Exit:** creating a conflicting window shows conflict feedback; approvers can act;
approved windows render.

### T3.5 · → frontend-engineer · deps: T3.3
**Do:** My Items screen (what I have out, due dates, overdue highlights, quick check-in)
and wire the Asset Detail reserve / check-out / check-in actions (stubs from M1).
**Files:** `frontend/src/screens/myitems/*`, edits to Asset Detail.
**Exit:** a user sees their open items with due/overdue state and can check in quickly.

### T3.6 · → qa-test-engineer + code-reviewer · deps: T3.2, T3.3 · **milestone gate**
**Do:** F4/F5 acceptance tests: two overlapping active reservations rejected at the DB;
reservation limit enforced; approval flow honors `requires_approval` and scope; checkout/
checkin lifecycle correct; overdue flagged. Verify approve/override/checkin all audited.
code-reviewer gates the diff.
**Exit:** F4 + F5 met; review signed off.

> **Open question (Q10):** default reservation caps (max active per user, max days ahead,
> max duration). Configurable; seed documented defaults until confirmed.
