"""T2.3 — RLS, the low-stock partial index, and the DB-level ledger-integrity
backstops for the M2 consumables/stock app.

Five concerns, each a reversible ``RunSQL`` block:

1. **RLS (R4 backstop)** on every tenant-owned table T2.1 introduced
   (``stock_stock_item``, ``stock_stock_txn``, ``stock_reorder_request``), via
   the shared ``apps.tenancy.db.enable_rls_sql`` helper so the policy is
   byte-identical to every other tenant table — the app-level
   ``TenantScopedManager`` filter and the DB policy cannot drift. Fail-closed:
   no ``app.current_tenant`` GUC -> ``tenant_id = NULL`` -> zero rows.

2. **Low-stock partial index** (``docs/data-model.md`` §3):
   ``stock_item (tenant_id) WHERE quantity_on_hand <= reorder_threshold``. Backs
   ``GET /stock?low_stock`` and the M5 low-stock scan — the planner reads only
   the (small) set of already-low items instead of a full scan. The plain
   ``(tenant)`` composite baseline §3 also lists already ships in ``0001``.
   (T2.3 confirmed the planner picks this index via a non-forced
   ``EXPLAIN (ANALYZE, BUFFERS)`` over ~10k seeded items. The definitive
   perf/acceptance EXPLAIN at realistic scale is owned by **T2.5**, the F3
   perf/acceptance task.)

3. **Ledger <-> quantity reconciliation at the DB** — the app can NEVER silently
   desync ``quantity_on_hand`` from the ``StockTxn`` ledger, regardless of the
   writer. See the TRIGGER DESIGN block below.

4. **``StockTxn`` immutability** (append-only): a trigger rejecting UPDATE/DELETE
   on ``stock_stock_txn`` outright — the DB backstop for the app-layer
   ``StockTxn.save()``/``delete()`` guard, mirroring the AuditLog approach. A
   "correction" is always a NEW insert.

5. **CHECK (quantity_on_hand >= 0)** and the **consumable-only** backstop (a
   ``stock_stock_item`` may only reference an ``assets_asset`` with
   ``is_consumable = true``) — the DB backstops for the app-layer guards in
   ``apps.stock.models``.

===========================================================================
TRIGGER DESIGN (own it) — how each invariant is GUARANTEED
===========================================================================

Two facts anchor the whole design:
  * the ledger (``stock_stock_txn``) is AUTHORITATIVE — ``quantity_on_hand`` is a
    derived cache of ``COALESCE(SUM(delta), 0)`` (``docs/data-model.md`` §2), and
  * a ``StockTxn`` row is IMMUTABLE — it can only ever be INSERTed (concern 4).
So the ONLY way the ledger sum for an item changes is a new INSERT, and the only
way ``quantity_on_hand`` changes is an UPDATE of ``stock_stock_item``. We pin
both edges:

--- (a) reconcile on ledger INSERT --- ``stock_txn_reconcile_quantity()``
AFTER INSERT on ``stock_stock_txn``: locks the parent item row FOR UPDATE, then
sets ``quantity_on_hand = COALESCE(SUM(delta), 0)`` over the item's ledger. This
makes the ledger authoritative no matter who writes the txn (the T2.2 service, a
stray raw INSERT, a future path): quantity always ends equal to the new sum.

  * Concurrency / why the explicit ``FOR UPDATE`` first, AND its exact scope:
    the ``PERFORM ... FOR UPDATE`` serializes writers on the parent item's row
    lock; because it is a SEPARATE statement, the following ``UPDATE``'s subquery
    runs under a FRESH READ COMMITTED snapshot taken AFTER the lock is granted,
    so its ``SUM`` sees the other writer's already-committed txn -> the second
    writer computes the correct total, never a lost update. This deadlock- AND
    lost-update-free guarantee holds specifically for writers that take
    ``FOR UPDATE`` on the StockItem BEFORE inserting the txn — i.e. the sanctioned
    T2.2 ``apply_stock_txn`` path, which does exactly that ``select_for_update``.
    A second concurrent ``apply_stock_txn`` blocks cleanly on that up-front lock
    and never co-holds a conflicting lock with a pending upgrade; and because the
    lock is re-entrant within the same transaction, this trigger's ``FOR UPDATE``
    is a no-op there (trigger and service agree, they don't fight).

    CAVEAT (deadlock, NOT corruption) for direct-insert writers: inserting a
    StockTxn row itself takes a ``FOR KEY SHARE`` lock on the parent StockItem
    (via the FK), and this trigger then tries to UPGRADE that to ``FOR UPDATE``.
    Two concurrent writers that insert a txn for the SAME item WITHOUT first
    taking ``FOR UPDATE`` on the item can therefore deadlock on the mutual
    lock-upgrade (Postgres detects it and aborts one transaction with a
    serialization/deadlock error — NO data corruption, the invariant is never
    violated). This is NOT reachable through ``apply_stock_txn`` (its up-front
    ``select_for_update`` prevents co-holding ``FOR KEY SHARE`` with a pending
    upgrade). REQUIREMENT for any FUTURE direct-insert path (bulk-import, etc.):
    it MUST either route through ``apply_stock_txn`` or take
    ``select_for_update()`` on the StockItem before inserting the StockTxn.
  * Consistency with ``apply_stock_txn``: the service, still under its lock,
    then issues ``UPDATE ... SET quantity_on_hand = recompute_quantity_on_hand()``
    (also ``= SUM(delta)``) — the SAME value this trigger already wrote, so the
    validation trigger (b) passes and the two writes agree. Belt and suspenders,
    not a tug-of-war.

--- (b) validate on quantity UPDATE --- ``stock_item_validate_quantity()``
BEFORE UPDATE on ``stock_stock_item``: whenever ``quantity_on_hand`` changes, it
must equal the ledger ``SUM(delta)``, else RAISE. This closes the other edge —
a raw ``UPDATE stock_stock_item SET quantity_on_hand = <wrong>`` is REJECTED. It
fires only when the column actually changes (``IS DISTINCT FROM OLD``), so
unrelated updates (threshold, bin_location, ``updated_at``) don't pay for a SUM,
and it can never make a correct row wrong. Inductive proof of the invariant:
the backfill leaves every row correct; every subsequent transition — a ledger
INSERT (fixed by (a) to the new sum, which (b) then accepts) or any item UPDATE
(accepted by (b) only if it equals the sum) — preserves ``quantity_on_hand ==
SUM(delta)``. There is no third way to change either side (txn UPDATE/DELETE are
blocked by concern 4). Airtight.

--- Recursion / deadlock safety ---
(a)'s ``UPDATE stock_stock_item`` fires (b) and the consumable trigger (c). (b)
only SELECTs the ledger (no write); (c) fires only ``UPDATE OF asset_id`` (not
touched here) so it doesn't run at all. Neither inserts a txn, so (a) never
re-fires -> no recursion. Lock order is uniform (every path locks the single
parent item row and nothing else in these triggers), so concurrent writers just
serialize on that one row — no lock cycle, no deadlock.

--- SECURITY INVOKER + RLS reasoning (same as T1.3) ---
All functions are ``SECURITY INVOKER``. At runtime they fire on the non-superuser
``cortex_app`` connection with RLS active and ``app.current_tenant`` set by the
request, so every read/write inside them is itself RLS-constrained to the current
tenant. An item and its txns share one tenant, and the triggering statement can
only touch current-tenant rows, so the reconciliation ``SUM`` always sees the
COMPLETE ledger for that item -> a correct total; it is structurally impossible
to read or write another tenant's rows. Fail-CLOSED. ``SECURITY DEFINER`` (run as
owner ``cortex``) would BYPASS RLS and let a mistaken future WHERE aggregate or
overwrite across tenants — a leak. Rejected. The consumable check (c) likewise
reads ``assets_asset`` as INVOKER: a cross-tenant asset is invisible under RLS ->
reads NULL -> ``IS DISTINCT FROM true`` -> rejected (fail-closed), never a silent
pass. During the in-migration backfill everything runs as the owner and bypasses
RLS, which is fine and intended — every query keys strictly by id, so each item's
quantity is still summed only from its OWN ledger rows.

Everything is reversible and idempotent-safe to re-run (``CREATE OR REPLACE`` /
``DROP ... IF EXISTS`` / ``IF NOT EXISTS`` / existence-guarded ``ADD CONSTRAINT``).
"""
from __future__ import annotations

from django.db import migrations

from apps.tenancy.db import disable_rls_sql, enable_rls_sql

STOCK_TENANT_TABLES = [
    "stock_stock_item",
    "stock_stock_txn",
    "stock_reorder_request",
]

# --- 2. low-stock partial index (docs/data-model.md §3) --------------------
INDEX_FORWARD = """
CREATE INDEX IF NOT EXISTS stock_stock_item_low_stock_partial
    ON stock_stock_item (tenant_id)
    WHERE quantity_on_hand <= reorder_threshold;
"""

INDEX_REVERSE = """
DROP INDEX IF EXISTS stock_stock_item_low_stock_partial;
"""

# --- 3 + 4 + consumable check: functions & triggers ------------------------
TRIGGERS_FORWARD = """
-- (a) Ledger is authoritative: after every txn INSERT, set the item's
--     quantity_on_hand to the ledger sum. FOR UPDATE first to serialize
--     concurrent writers so the following (fresh-snapshot) SUM is never a
--     lost update. See the migration module docstring for the full design.
CREATE OR REPLACE FUNCTION stock_txn_reconcile_quantity()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
AS $$
BEGIN
    PERFORM 1 FROM stock_stock_item WHERE id = NEW.stock_item_id FOR UPDATE;
    UPDATE stock_stock_item
        SET quantity_on_hand = COALESCE((
            SELECT SUM(delta) FROM stock_stock_txn
            WHERE stock_item_id = NEW.stock_item_id
        ), 0)
    WHERE id = NEW.stock_item_id;
    RETURN NULL;  -- AFTER trigger: return value ignored
END;
$$;

-- (b) quantity_on_hand may only ever equal the ledger sum. Rejects any UPDATE
--     that would leave it diverged. Fires only when the column changes.
CREATE OR REPLACE FUNCTION stock_item_validate_quantity()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
AS $$
DECLARE
    v_sum bigint;
BEGIN
    IF NEW.quantity_on_hand IS DISTINCT FROM OLD.quantity_on_hand THEN
        SELECT COALESCE(SUM(delta), 0) INTO v_sum
        FROM stock_stock_txn
        WHERE stock_item_id = NEW.id;
        IF NEW.quantity_on_hand <> v_sum THEN
            RAISE EXCEPTION
                'stock_stock_item.quantity_on_hand (%) must equal the StockTxn '
                'ledger sum (%) for stock_item % (docs/data-model.md §2)',
                NEW.quantity_on_hand, v_sum, NEW.id
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

-- (c) Consumable-only backstop: a StockItem may only extend a consumable asset
--     (a CHECK can't cross tables). Fires on INSERT and on asset_id re-point.
CREATE OR REPLACE FUNCTION stock_item_require_consumable_asset()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
AS $$
DECLARE
    v_is_consumable boolean;
BEGIN
    SELECT is_consumable INTO v_is_consumable
    FROM assets_asset
    WHERE id = NEW.asset_id;
    IF v_is_consumable IS DISTINCT FROM true THEN
        RAISE EXCEPTION
            'stock_stock_item.asset_id % must reference a consumable asset '
            '(assets_asset.is_consumable = true; docs/data-model.md §4)',
            NEW.asset_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

-- (4) StockTxn immutability: the ledger is append-only. Reject UPDATE/DELETE
--     outright (mirrors the AuditLog immutability approach).
CREATE OR REPLACE FUNCTION stock_txn_forbid_mutation()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
AS $$
BEGIN
    RAISE EXCEPTION
        'stock_stock_txn is append-only: % is not permitted. Write a new row '
        '(reason=''correction'') instead (docs/data-model.md §2).',
        TG_OP
        USING ERRCODE = 'restrict_violation';
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS stock_txn_reconcile_ai ON stock_stock_txn;
CREATE TRIGGER stock_txn_reconcile_ai
    AFTER INSERT ON stock_stock_txn
    FOR EACH ROW EXECUTE FUNCTION stock_txn_reconcile_quantity();

DROP TRIGGER IF EXISTS stock_txn_immutable_bud ON stock_stock_txn;
CREATE TRIGGER stock_txn_immutable_bud
    BEFORE UPDATE OR DELETE ON stock_stock_txn
    FOR EACH ROW EXECUTE FUNCTION stock_txn_forbid_mutation();

DROP TRIGGER IF EXISTS stock_item_validate_qty_bu ON stock_stock_item;
CREATE TRIGGER stock_item_validate_qty_bu
    BEFORE UPDATE ON stock_stock_item
    FOR EACH ROW EXECUTE FUNCTION stock_item_validate_quantity();

DROP TRIGGER IF EXISTS stock_item_consumable_biu ON stock_stock_item;
CREATE TRIGGER stock_item_consumable_biu
    BEFORE INSERT OR UPDATE OF asset_id ON stock_stock_item
    FOR EACH ROW EXECUTE FUNCTION stock_item_require_consumable_asset();
"""

TRIGGERS_REVERSE = """
DROP TRIGGER IF EXISTS stock_item_consumable_biu ON stock_stock_item;
DROP TRIGGER IF EXISTS stock_item_validate_qty_bu ON stock_stock_item;
DROP TRIGGER IF EXISTS stock_txn_immutable_bud ON stock_stock_txn;
DROP TRIGGER IF EXISTS stock_txn_reconcile_ai ON stock_stock_txn;
DROP FUNCTION IF EXISTS stock_txn_forbid_mutation();
DROP FUNCTION IF EXISTS stock_item_require_consumable_asset();
DROP FUNCTION IF EXISTS stock_item_validate_quantity();
DROP FUNCTION IF EXISTS stock_txn_reconcile_quantity();
"""

# --- 5a. backfill quantity_on_hand to the ledger sum (owner; bypasses RLS) --
# Runs after the triggers exist: the validation trigger (b) fires on this
# UPDATE and passes (we set exactly the ledger sum); the consumable trigger (c)
# does not fire (asset_id untouched). Reverse is a documented no-op: the value
# written IS the correct ledger sum, so there is no prior (wrong) state worth
# restoring — reversing this migration only removes the enforcement, it does not
# need to un-correct data.
BACKFILL_FORWARD = """
UPDATE stock_stock_item si
    SET quantity_on_hand = COALESCE((
        SELECT SUM(delta) FROM stock_stock_txn t WHERE t.stock_item_id = si.id
    ), 0);
"""

# --- 5b. non-negative CHECK backstop ---------------------------------------
CHECK_FORWARD = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'stock_stock_item_qoh_nonneg'
    ) THEN
        ALTER TABLE stock_stock_item
            ADD CONSTRAINT stock_stock_item_qoh_nonneg
            CHECK (quantity_on_hand >= 0);
    END IF;
END$$;
"""

CHECK_REVERSE = """
ALTER TABLE stock_stock_item DROP CONSTRAINT IF EXISTS stock_stock_item_qoh_nonneg;
"""


class Migration(migrations.Migration):

    dependencies = [
        ("stock", "0001_initial"),
    ]

    operations = [
        # 1. RLS on every new tenant-owned table.
        *[
            migrations.RunSQL(
                sql=enable_rls_sql(table),
                reverse_sql=disable_rls_sql(table),
            )
            for table in STOCK_TENANT_TABLES
        ],
        # 2. Low-stock partial index (data-model.md §3).
        migrations.RunSQL(sql=INDEX_FORWARD, reverse_sql=INDEX_REVERSE),
        # 3 + 4 + consumable check: functions & triggers.
        migrations.RunSQL(sql=TRIGGERS_FORWARD, reverse_sql=TRIGGERS_REVERSE),
        # 5a. Backfill existing quantities to the ledger sum (owner role).
        migrations.RunSQL(sql=BACKFILL_FORWARD, reverse_sql=migrations.RunSQL.noop),
        # 5b. Non-negative CHECK backstop.
        migrations.RunSQL(sql=CHECK_FORWARD, reverse_sql=CHECK_REVERSE),
    ]
