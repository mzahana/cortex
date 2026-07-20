"""The `low_stock_crossed` domain event (T2.2, `docs/features.md` F9's
"low-stock alert" email event; `docs/architecture.md` §6: "business logic
emits *domain events* ... it never imports Brevo directly").

## Why a `django.dispatch.Signal`, not a DB outbox table

M5 (notifications) does not exist yet, and this task explicitly must not
implement email or import Brevo. `docs/architecture.md` §6 describes the
contract as "business logic emits domain events that map to templated
emails" — a plain, well-documented Django signal is the standard, in-process
mechanism for exactly that, needs no schema/migration (out of scope for a
backend-engineer task — schema changes are db-migration-specialist's, and an
outbox table is a schema change), and is trivial for M5 to connect a
`@receiver` to (a Celery task that enqueues the actual Brevo send). If M5
instead wants durability across a worker crash between "signal sent" and
"task enqueued", the receiver it registers can itself write an outbox/
`EmailLog`-style row in the same handler — this event is the trigger, not
the durability boundary; T2.3/M5 own that call.

## Durability of THIS emission

`apply_stock_txn` (`apps.stock.services`) sends this signal from inside a
`transaction.on_commit()` callback, not synchronously inside the DB
transaction. That means:
- it never fires for a txn that gets rolled back (e.g. the negative-quantity
  guard raising mid-transaction), and
- by the time it fires, the `StockTxn` row and the new `quantity_on_hand`
  are already durably committed — so any receiver that re-reads the stock
  item (e.g. to render an email) sees consistent, already-committed state,
  never a value from a transaction that might still abort.

## Contract for M5 (or any other subscriber)

Connect with `@receiver(low_stock_crossed)`. Sent via `send_robust` (a
receiver raising never breaks the stock-txn request/task that triggered
it). Keyword arguments provided (`sender=StockItem`):

| kwarg               | type       | meaning                                             |
|---------------------|------------|------------------------------------------------------|
| `stock_item_id`     | `int`      | the `StockItem.id` that crossed into low-stock       |
| `tenant_id`          | `int`      | tenant, for scoping the notification / email prefs   |
| `asset_id`           | `int`      | the underlying consumable `Asset.id`                 |
| `previous_quantity`  | `int`      | `quantity_on_hand` immediately before this txn        |
| `new_quantity`       | `int`      | `quantity_on_hand` right after (`<= reorder_threshold`) |
| `reorder_threshold`  | `int`      | the threshold that was crossed                        |
| `txn_id`             | `int`      | the `StockTxn.id` whose apply caused the crossing     |
| `occurred_at`        | `datetime` | wall-clock time the signal was sent (post-commit)     |

**Crossing-edge semantics (idempotent — fires once per crossing, not once
per low-stock txn):** emitted ONLY when `previous_quantity` was strictly
ABOVE `reorder_threshold` and `new_quantity` is AT-OR-BELOW it. A second
`consume` txn while the item is ALREADY at/under threshold does not re-fire
(`previous_quantity` is already `<= reorder_threshold`, so the "was above"
half of the edge condition is false). A `receive`/correction that lifts the
quantity back above the threshold and a later txn that drops it below again
DOES re-fire — that is a fresh crossing, not a repeat of the same one.
"""

from __future__ import annotations

import django.dispatch

low_stock_crossed = django.dispatch.Signal()
