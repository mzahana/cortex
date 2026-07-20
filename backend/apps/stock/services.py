"""Business logic for the stock endpoints (T2.2): the concurrency-safe
ledger-apply path and the reorder-request transition/approval path.

Kept out of `apps.stock.api` (thin views, `CLAUDE.md`/`docs/rbac.md`
convention) so the atomicity/locking contract has exactly one implementation
that both the endpoint and any future caller (e.g. a bulk-receive import in a
later milestone) shares.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from .models import ReorderRequest, StockItem, StockTxn
from .signals import low_stock_crossed


@dataclass
class StockTxnResult:
    stock_item: StockItem
    txn: StockTxn
    previous_quantity: int
    new_quantity: int

    @property
    def crossed_into_low_stock(self) -> bool:
        threshold = self.stock_item.reorder_threshold
        return self.previous_quantity > threshold and self.new_quantity <= threshold


def apply_stock_txn(
    *,
    stock_item: StockItem,
    delta: int,
    reason: str,
    ref: str,
    actor,
) -> StockTxnResult:
    """Append one `StockTxn` row and reconcile `StockItem.quantity_on_hand` to
    the ledger sum, atomically and concurrency-safely.

    **Concurrency:** the whole read-modify-write is wrapped in
    `transaction.atomic()` with `select_for_update()` taken on the
    `StockItem` row FIRST — before the `StockTxn` insert or the
    `quantity_on_hand` update — so two concurrent txns against the SAME
    stock item serialize on that row lock: the second transaction blocks
    until the first commits (or rolls back), then re-reads
    `quantity_on_hand`/re-sums the ledger with the first txn's row already
    visible. This is what makes "quantity always reconciles to the ledger
    sum" hold even under concurrent writers, not just sequential ones (no
    lost-update race between the ledger insert and the derived-quantity
    write). The lock is released at commit, when the response is already
    being built — never held across a network round-trip.

    **Reconciliation, not increment-under-lock:** after inserting the new
    ledger row, `quantity_on_hand` is set from
    `stock_item.recompute_quantity_on_hand()` (a fresh `SUM(delta)` over the
    ledger) rather than `quantity_on_hand + delta`. Both are equivalent
    under the row lock (no concurrent writer can have changed the ledger sum
    between the two reads), but re-summing is the single definition of
    "correct" this module and `docs/data-model.md` §2 agree on, so it can
    never silently drift from the ledger even if some future code path ever
    writes a `StockTxn` without going through this function. T2.3 layers a
    DB-level trigger/constraint enforcing this same reconciliation
    permanently as the backstop.

    **Negative-quantity guard (flagged assumption):** `docs/data-model.md`
    doesn't say whether the ledger may go negative; physical stock cannot,
    so ANY txn (any reason, including `correction`) that would drive the
    reconciled quantity below zero is rejected with a 400
    (`serializers.ValidationError`). T2.3 added a DB-level
    `CHECK (quantity_on_hand >= 0)` PLUS an `AFTER INSERT` trigger on
    `StockTxn` that reconciles `quantity_on_hand` INSIDE the insert
    statement itself — so by the time a negative-sum `StockTxn` row would
    exist, the CHECK has already fired mid-INSERT as a raw
    `IntegrityError`, which is too late for a clean 400 (it would surface
    as an unhandled 500). This guard therefore runs its arithmetic
    would-be-quantity check BEFORE calling `StockTxn.objects.create(...)`
    at all (see the code below), under the same row lock, so the
    sanctioned path here never actually reaches the trigger/CHECK with a
    negative sum — the DB CHECK stays pure defense-in-depth for any path
    that bypasses this service.

    **`low_stock` event:** emitted (via `transaction.on_commit`, so only
    once this txn durably commits) exactly when this apply crosses the
    reorder threshold edge — see `apps.stock.signals.low_stock_crossed`'s
    module docstring for the exact contract and idempotency rule.
    """
    with transaction.atomic():
        locked_item = StockItem.objects.select_for_update().get(pk=stock_item.pk)
        previous_quantity = locked_item.quantity_on_hand

        # **Pre-check BEFORE the `StockTxn` insert (T2.3 follow-up).** T2.3
        # added a DB-level `AFTER INSERT` trigger on `stock_stock_txn` that
        # reconciles `quantity_on_hand` to the new ledger sum INSIDE the
        # `StockTxn.objects.create()` statement itself, plus a
        # `CHECK (quantity_on_hand >= 0)` on `stock_stock_item`. If the
        # resulting sum were negative, that CHECK would fire mid-INSERT as
        # an `IntegrityError` the caller never gets a clean chance to catch
        # as a 400 — DRF's default handling would surface it as a 500. So
        # this guard computes the would-be quantity from arithmetic
        # (`previous_quantity + delta`, valid because `quantity_on_hand` is
        # always kept equal to the ledger sum by this same invariant — see
        # the T2.3 migration's module docstring) and rejects BEFORE the
        # insert ever reaches the trigger/CHECK, under the SAME row lock
        # already held above (no race between this check and the insert).
        # The DB CHECK stays untouched as pure defense-in-depth for any path
        # that bypasses this service (e.g. a stray raw INSERT) — it should
        # never actually trip via the sanctioned path.
        would_be_quantity = previous_quantity + delta
        if would_be_quantity < 0:
            raise serializers.ValidationError(
                {
                    "delta": (
                        "This transaction would drive quantity_on_hand negative "
                        f"({would_be_quantity}); rejected."
                    )
                }
            )

        txn = StockTxn.objects.create(
            tenant=locked_item.tenant,
            stock_item=locked_item,
            delta=delta,
            reason=reason,
            ref=ref,
            actor=actor,
        )

        # The T2.3 trigger already reconciled `quantity_on_hand` to the new
        # ledger sum as a side effect of the INSERT above; this re-fetches
        # (rather than trusting `would_be_quantity`) so `new_quantity` is
        # always exactly what the DB now holds, and re-issues the SAME
        # value via `.save()` — belt-and-suspenders with the trigger, not a
        # tug-of-war (T2.3 migration docstring), and what the (b) validation
        # trigger expects to see (a quantity update that already equals the
        # ledger sum).
        new_quantity = locked_item.recompute_quantity_on_hand()
        locked_item.quantity_on_hand = new_quantity
        locked_item.save(update_fields=["quantity_on_hand", "updated_at"])

        result = StockTxnResult(
            stock_item=locked_item,
            txn=txn,
            previous_quantity=previous_quantity,
            new_quantity=new_quantity,
        )

        if result.crossed_into_low_stock:
            _emit_low_stock_event_on_commit(result)

    return result


def _emit_low_stock_event_on_commit(result: StockTxnResult) -> None:
    stock_item = result.stock_item
    txn = result.txn
    previous_quantity = result.previous_quantity
    new_quantity = result.new_quantity

    def _send() -> None:
        low_stock_crossed.send_robust(
            sender=StockItem,
            stock_item_id=stock_item.id,
            tenant_id=stock_item.tenant_id,
            asset_id=stock_item.asset_id,
            previous_quantity=previous_quantity,
            new_quantity=new_quantity,
            reorder_threshold=stock_item.reorder_threshold,
            txn_id=txn.id,
            occurred_at=timezone.now(),
        )

    transaction.on_commit(_send)


def apply_reorder_transition(
    *,
    reorder_request: ReorderRequest,
    new_status: str,
    actor,
) -> ReorderRequest:
    """Validate + apply a `ReorderRequest` status transition
    (`ReorderRequest.VALID_TRANSITIONS` is the single source of truth,
    re-checked here — not just in the serializer — so this function is safe
    to call from any future caller, not only the DRF `PATCH` path).

    Sets `approved_by`/`decided_at` only for the `approved` transition (the
    actual `reorder.approve` decision point, `docs/rbac.md` §5); other
    forward transitions (`ordered`, `received`, `cancelled`) do not touch
    those fields — cancelling/receiving isn't "approving", per the task's
    audit-scope instruction ("every reorder **approval** writes an
    AuditLog entry", not every transition). Auditing the approval itself is
    left to the caller (`apps.stock.api`), which has the request/ip context
    `write_audit_log` needs.
    """
    reorder_request.validate_transition(new_status)

    reorder_request.status = new_status
    update_fields = ["status", "updated_at"]
    if new_status == ReorderRequest.Status.APPROVED:
        reorder_request.approved_by = actor
        reorder_request.decided_at = timezone.now()
        update_fields += ["approved_by", "decided_at"]

    reorder_request.save(update_fields=update_fields)
    return reorder_request
