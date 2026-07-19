# M2 — Consumables & stock (MVP)

**Goal:** consumable quantity tracking on an immutable ledger, low-stock detection, and
the reorder workflow, with UI. Runs in parallel with M3 after M1.
**Dep:** M1. **Effort:** M.
**Milestone exit:** F3 acceptance met.
Refs: `docs/data-model.md` (StockItem/StockTxn/ReorderRequest), `docs/features.md` F3,
`docs/rbac.md`, `docs/api-and-ui.md`.

---

### T2.1 · → backend-engineer · deps: M1
**Do:** StockItem (1:1 extension of a consumable Asset: unit_of_measure,
quantity_on_hand, reorder_threshold, reorder_target, bin_location_id), StockTxn
(immutable ledger: delta, reason receive|consume|adjust|correction, ref, actor), and
ReorderRequest (status open→approved→ordered→received→cancelled). Enforce the
consumable/durable invariant: only consumable assets get a StockItem.
**Files:** `backend/apps/stock/`.
**Exit:** models + tenant-scoped serializers; a consumable Asset can own exactly one StockItem.

### T2.2 · → backend-engineer · deps: T2.1
**Do:** Stock endpoints from `docs/api-and-ui.md`: `GET /stock` (with low-stock filter),
`POST /stock/{id}/txn` (receive/consume/adjust — writes a ledger row and adjusts
quantity atomically), `GET/POST/PATCH /reorder-requests`. Enforce RBAC keys
(`stock.adjust`, `stock.consume`, `reorder.request`, `reorder.approve` scoped). Quantity
is **derived/checked against the ledger**, never set directly. Every `stock.adjust` and
reorder approval writes an AuditLog entry. Emit a `low_stock` domain event when a txn
crosses the reorder threshold (consumed by M5 email).
**Files:** `backend/apps/stock/api.py`, `backend/apps/stock/services.py`.
**Exit:** consuming stock decrements via a ledger txn; crossing threshold flags low-stock
and emits the event; unauthorized adjust → 403.

### T2.3 · → db-migration-specialist · deps: T2.2
**Do:** Migrations for T2.1/T2.2. Partial index `stock_item (tenant_id)` where
`quantity_on_hand <= reorder_threshold` (low-stock scan). RLS on new tables. Add a
constraint/trigger ensuring `quantity_on_hand` reconciles to the StockTxn ledger sum so
the app cannot silently desync it.
**Files:** `backend/apps/stock/migrations/`.
**Exit:** up/down clean; low-stock partial index used by the scan query (EXPLAIN);
ledger-reconciliation enforced at the DB.

### T2.4 · → frontend-engineer · deps: T2.2
**Do:** Stock / Consumables screen: quantities with low-stock highlights, receive/consume
actions, and the reorder-request flow. Actions gated by `/me` permissions.
**Files:** `frontend/src/screens/stock/*`.
**Exit:** receive/consume update quantity live; low-stock items highlighted; reorder
request can be raised and (by an approver) approved.

### T2.5 · → qa-test-engineer + code-reviewer · deps: T2.2, T2.3 · **milestone gate**
**Do:** F3 acceptance tests: consume decrements via ledger; quantity always reconciles to
the ledger sum (including under concurrent txns); threshold crossing flags low-stock and
emits the event; a correction is an immutable new row, not an edit. code-reviewer gates
the diff (audit completeness + tenant scoping + ledger integrity).
**Exit:** F3 met; review signed off.
