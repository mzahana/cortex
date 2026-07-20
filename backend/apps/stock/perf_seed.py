"""T2.5 — reusable seed for the low-stock partial index EXPLAIN proof at
realistic scale (the gap T2.3's own review flagged: T2.3 confirmed the
partial index EXISTS and is usable, but only checked it at small scale; the
definitive "does the planner naturally pick it at 1000s of rows" proof is
owned here, mirroring the rigor of `apps.assets.perf_seed`/
`apps.assets.tests.test_perf_10k`).

Builds `count` consumable `Asset` + `StockItem` rows (a fixed mix: ~15% seeded
already at-or-under their `reorder_threshold`, ~85% comfortably healthy) plus
one `StockTxn` (`reason=receive`) per item establishing that starting
quantity, so every row's `quantity_on_hand` is ledger-reconciled from the
moment it exists (same invariant as the app path, `apps.stock.services.
apply_stock_txn`) — never poked directly.

**Why triggers are disabled around the bulk load (same reasoning as
`apps.assets.perf_seed`):** both the `assets_asset` search-vector trigger and
the `stock_stock_txn` ledger-reconciliation trigger (T2.3) are `FOR EACH ROW`
— naively bulk-inserting thousands of rows would still pay a per-row
UPDATE-with-subquery cost for the reconciliation trigger specifically (it
locks the parent `StockItem` row and re-sums its whole ledger on EVERY
`StockTxn` insert). Disabling it around the bulk load and doing exactly ONE
set-based backfill afterward (identical SQL shape to migration 0002's own
`BACKFILL_FORWARD`) produces byte-identical `quantity_on_hand` values to what
the trigger would have produced row-by-row, just computed once instead of
`count` times. Must run on the OWNER connection (only the table owner may
disable a trigger) — same requirement `apps.assets.perf_seed` documents.

**Cleanup is the interesting part.** `stock_stock_txn` also carries the
IMMUTABILITY trigger (T2.3): `BEFORE UPDATE OR DELETE ... RAISE EXCEPTION`.
That trigger does not care whether the DELETE arrives directly or via an
`Asset`/`StockItem` cascade — it is `FOR EACH ROW`, so it fires (and blocks)
either way. `delete_seeded_tenant` below therefore disables JUST that
immutability trigger for the duration of its own cleanup delete, the same
owner-only, always-re-enabled-in-a-`finally` pattern as the bulk-load side.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from django.db import connection, transaction

from apps.catalog.models import Category
from apps.tenancy.models import Tenant

from .models import StockItem, StockTxn

_ASSET_SEARCH_VECTOR_TRIGGER = ("assets_asset", "assets_asset_search_vector_biu")
_LEDGER_RECONCILE_TRIGGER = ("stock_stock_txn", "stock_txn_reconcile_ai")
_LEDGER_IMMUTABLE_TRIGGER = ("stock_stock_txn", "stock_txn_immutable_bud")

REORDER_THRESHOLD = 10
LOW_STOCK_FRACTION = 0.15  # ~15% of seeded items start at-or-under threshold


def _set_trigger(table: str, trigger: str, *, enabled: bool) -> None:
    verb = "ENABLE" if enabled else "DISABLE"
    with connection.cursor() as cur:
        cur.execute(f"ALTER TABLE {table} {verb} TRIGGER {trigger};")


class _trigger_disabled:  # noqa: N801 - context manager used as a verb
    def __init__(self, table: str, trigger: str) -> None:
        self._table = table
        self._trigger = trigger

    def __enter__(self) -> None:
        _set_trigger(self._table, self._trigger, enabled=False)

    def __exit__(self, *exc_info: object) -> None:
        _set_trigger(self._table, self._trigger, enabled=True)


_BACKFILL_SQL = """
UPDATE stock_stock_item si
    SET quantity_on_hand = COALESCE((
        SELECT SUM(delta) FROM stock_stock_txn t WHERE t.stock_item_id = si.id
    ), 0)
    WHERE si.tenant_id = %s;
"""


@dataclass
class StockPerfSeedResult:
    tenant_id: int
    category_id: int
    stock_items_created: int = 0
    low_stock_count: int = 0
    healthy_count: int = 0


def seed_stock_items(
    tenant: Tenant,
    *,
    count: int = 8_000,
    rng_seed: int = 7,
    batch_size: int = 1_000,
) -> StockPerfSeedResult:
    """Bulk-create `count` consumable `Asset` + `StockItem` (+ one seeding
    `StockTxn` each) rows for `tenant`, at realistic scale for the low-stock
    partial index EXPLAIN proof. Must run on a connection with owner
    privileges (see module docstring) — pytest-django's default connection
    qualifies, matching `apps.assets.perf_seed`'s own requirement.
    """
    from apps.assets.models import Asset  # local import: avoid an app-loading cycle

    rng = random.Random(rng_seed)
    category, _ = Category.all_objects.get_or_create(
        tenant=tenant,
        parent=None,
        name="Perf Consumables",
        defaults={"default_is_consumable": True},
    )

    result = StockPerfSeedResult(tenant_id=tenant.id, category_id=category.id)

    with _trigger_disabled(*_ASSET_SEARCH_VECTOR_TRIGGER):
        with transaction.atomic():
            pending_assets = []
            for i in range(count):
                pending_assets.append(
                    Asset(
                        tenant_id=tenant.id,
                        category=category,
                        name=f"Perf Consumable {i:06d}",
                        is_consumable=True,
                        serial_number=f"SN-PERFSTOCK-{i:06d}",
                    )
                )
            Asset.all_objects.bulk_create(pending_assets, batch_size=batch_size)

    # Re-fetch in insertion order (bulk_create doesn't reliably return PKs on
    # every backend, but Postgres does; re-querying is the portable way to be
    # sure we have exactly the rows just created, in a stable order).
    assets = list(Asset.all_objects.filter(tenant=tenant, category=category).order_by("id"))
    assert len(assets) == count

    pending_items = []
    for asset in assets:
        pending_items.append(
            StockItem(
                tenant_id=tenant.id,
                asset=asset,
                unit_of_measure="unit",
                quantity_on_hand=0,  # reconciled by the backfill below
                reorder_threshold=REORDER_THRESHOLD,
                reorder_target=REORDER_THRESHOLD * 4,
            )
        )
    StockItem.all_objects.bulk_create(pending_items, batch_size=batch_size)
    result.stock_items_created = len(pending_items)

    items = list(
        StockItem.all_objects.filter(tenant=tenant, asset__category=category).order_by("id")
    )
    assert len(items) == count

    with _trigger_disabled(*_LEDGER_RECONCILE_TRIGGER):
        with transaction.atomic():
            pending_txns = []
            for item in items:
                if rng.random() < LOW_STOCK_FRACTION:
                    quantity = rng.randint(0, REORDER_THRESHOLD)  # at-or-under threshold
                    result.low_stock_count += 1
                else:
                    quantity = rng.randint(REORDER_THRESHOLD + 1, REORDER_THRESHOLD * 20)
                    result.healthy_count += 1
                pending_txns.append(
                    StockTxn(
                        tenant_id=tenant.id,
                        stock_item=item,
                        delta=quantity,
                        reason=StockTxn.Reason.RECEIVE,
                        ref="perf-seed",
                    )
                )
            StockTxn.all_objects.bulk_create(pending_txns, batch_size=batch_size)

    with connection.cursor() as cur:
        cur.execute(_BACKFILL_SQL, [tenant.id])

    # VACUUM (ANALYZE), not just ANALYZE — the backfill above is a single
    # UPDATE touching every just-bulk_create'd row's quantity_on_hand (an
    # indexed column via the partial index -> never a HOT update), leaving
    # dead tuples / stale index entries that would otherwise skew the
    # EXPLAIN this seed exists to support (same finding as
    # `apps.assets.perf_seed`). Must run outside any open transaction.
    with connection.cursor() as cur:
        cur.execute("VACUUM (ANALYZE) stock_stock_item;")
        cur.execute("VACUUM (ANALYZE) stock_stock_txn;")
        cur.execute("VACUUM (ANALYZE) assets_asset;")

    return result


def delete_seeded_stock_tenant(tenant: Tenant) -> None:
    """Undo `seed_stock_items` (+ the `Tenant`/admin membership `perf_corpus`
    fixtures typically also create), in FK-safe order. Must temporarily
    disable the `StockTxn` immutability trigger (see module docstring) —
    without that, deleting (or cascading into) `stock_stock_txn` rows is
    rejected by the same DB-level backstop this task's F3 acceptance suite
    proves elsewhere.
    """
    from apps.assets.models import Asset  # local import: avoid an app-loading cycle
    from apps.rbac.models import Membership

    with _trigger_disabled(*_LEDGER_IMMUTABLE_TRIGGER):
        StockTxn.all_objects.filter(tenant=tenant).delete()
        StockItem.all_objects.filter(tenant=tenant).delete()
    Asset.all_objects.filter(tenant=tenant).delete()
    Category.all_objects.filter(tenant=tenant).delete()
    Membership.all_objects.filter(tenant=tenant).delete()
    Tenant.objects.filter(pk=tenant.id).delete()
