"""T2.5 — the M2 milestone-gate F3 acceptance suite.

`docs/features.md` F3 acceptance, verbatim: "Consuming stock decrements
quantity via a ledger txn; crossing the reorder threshold flags low-stock and
(F9) emails; quantity always reconciles to the ledger sum."
`docs/tasks/M2-consumables-stock.md` T2.5 additionally calls out: "including
under concurrent txns" and "a correction is an immutable new row, not an
edit."

This module does NOT re-prove every clause from scratch — most of F3 is
already covered elsewhere, and duplicating it would just make the suite
harder to trust. What's proven where:

  * "Consuming stock decrements quantity via a ledger txn" —
    `apps.stock.tests.test_stock_api.TestStockTxn.
    test_consume_decrements_quantity_via_ledger` (already covered).
  * "Quantity always reconciles to the ledger sum" (sequential path,
    receive/consume/adjust/correction all writing through the ledger) —
    `apps.stock.tests.test_stock_api.TestStockTxn` (several tests) and
    `apps.stock.tests.test_stock_models.TestStockTxnAppendOnly` (already
    covered).
  * "...including under concurrent txns", SAME-direction case (N concurrent
    `consume`s against one item never lose an update) —
    `apps.stock.tests.test_stock_api.TestStockTxn.
    test_concurrent_consume_txns_serialize_and_reconcile` (already
    covered). **Gap this module closes**: that test only exercises
    concurrent writers moving the ledger in the SAME direction; a mixed
    concurrent receive+consume workload is a materially different interleaving
    (some threads adding, some subtracting) and is NOT covered anywhere else
    — see `TestConcurrentMixedReceiveAndConsume` below.
  * "Crossing the reorder threshold flags low-stock ... and emits the event"
    (a single crossing, and non-refire while already below) —
    `apps.stock.tests.test_stock_api.TestStockTxn.
    test_low_stock_event_fires_once_on_crossing` /
    `test_low_stock_event_does_not_fire_on_a_rolled_back_txn` (already
    covered), and the `?low_stock=true` filter reflecting the crossed state
    — `apps.stock.tests.test_stock_api.TestStockList.test_low_stock_filter`
    (already covered), reconfirmed at 8k+ realistic scale in
    `apps.stock.tests.test_stock_perf_low_stock`. **Gap this module
    closes**: none of the above ever cross BACK above threshold and then
    below again — leaving open the question of whether the event is
    computed fresh from each txn's own before/after quantity (correct) or
    gated by a stateful "already notified once" flag (a plausible but wrong
    implementation that would never re-fire after recovery). See
    `TestLowStockCrossingBackAndForth` below.
  * "A correction is an immutable new row, not an edit" — app layer:
    `apps.stock.tests.test_stock_api.TestStockTxn.
    test_correction_is_a_new_row_not_an_edit` and `apps.stock.tests.
    test_stock_models.TestStockTxnAppendOnly.
    test_correction_is_a_new_row_not_an_edit` (already covered). **Gap this
    module closes** (the one code review flagged explicitly): none of the
    above exercise the DB-level trigger backstop via a RAW UPDATE that never
    goes through the ORM at all — see
    `apps.stock.tests.test_stock_db_immutability` (separate module, raw-SQL
    `app_role_connection` proof).

So this module contains exactly the two net-new scenarios: mixed-direction
concurrency, and multi-crossing low-stock event correctness.
"""

from __future__ import annotations

import json
import threading

import pytest
from django.db import connection

from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
    StockItemFactory,
    TenantFactory,
    UserFactory,
    upgrade_tenant_wide_role,
)
from apps.rbac.permission_keys import ROLE_ADMIN
from apps.stock.models import StockTxn
from apps.stock.services import apply_stock_txn
from apps.stock.signals import low_stock_crossed
from apps.tenancy.context import tenant_context

pytestmark = pytest.mark.django_db


def _seed_ledger(stock_item, *, quantity: int, actor=None) -> None:
    with tenant_context(stock_item.tenant_id):
        StockTxn.objects.create(
            tenant=stock_item.tenant,
            stock_item=stock_item,
            delta=quantity,
            reason=StockTxn.Reason.RECEIVE,
            actor=actor,
        )
        stock_item.quantity_on_hand = quantity
        stock_item.save(update_fields=["quantity_on_hand"])


class TestConcurrentMixedReceiveAndConsume:
    """Gap in `test_concurrent_consume_txns_serialize_and_reconcile`: that
    test only proves same-direction concurrent writers reconcile correctly.
    A MIXED workload (some threads receiving, some consuming, against the
    SAME item, at as close to the same wall-clock time as native threads
    allow) exercises the `select_for_update()` serialization point
    differently — some threads' "would-be quantity" pre-check
    (`apps.stock.services.apply_stock_txn`) depends on having observed a
    PRIOR concurrent receive, not just prior consumes — and this is where a
    subtly wrong lock-then-read ordering would show up as either a lost
    update (final quantity wrong) or a spurious negative-quantity rejection
    (a consume rejected because it raced ahead of a receive that should have
    made room for it, but under `select_for_update()` correctness the
    serialized order always gives every consume the full committed state so
    far — no such spurious rejection should occur here since deltas are
    chosen so no serialization order ever drives the running total
    negative).
    """

    def test_concurrent_mixed_receive_and_consume_reconciles(self, transactional_db):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        stock_item = StockItemFactory(tenant=tenant, reorder_threshold=0)
        _seed_ledger(stock_item, quantity=100, actor=admin)

        # Chosen so that regardless of serialization order, the running
        # total never dips below zero (min prefix sum over any permutation
        # is bounded below by -sum(|negative deltas|), comfortably above
        # -100 here) — a rejection here would indicate a real correctness
        # bug, not an expected race.
        deltas = [50, -20, 30, -40, 15, -10, 25, -5]
        expected_final = 100 + sum(deltas)

        barrier = threading.Barrier(len(deltas))
        errors: list[BaseException] = []

        def worker(delta: int) -> None:
            try:
                barrier.wait(timeout=5)
                reason = (
                    StockTxn.Reason.RECEIVE.value if delta > 0 else StockTxn.Reason.CONSUME.value
                )
                with tenant_context(tenant.id):
                    apply_stock_txn(
                        stock_item=stock_item,
                        delta=delta,
                        reason=reason,
                        ref="",
                        actor=admin,
                    )
            except BaseException as exc:  # pragma: no cover - surfaced via `errors`
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=worker, args=(d,)) for d in deltas]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert not errors, errors
        with tenant_context(tenant.id):
            stock_item.refresh_from_db()
            assert stock_item.quantity_on_hand == expected_final
            assert stock_item.quantity_on_hand == stock_item.recompute_quantity_on_hand()
            assert StockTxn.objects.filter(stock_item=stock_item).count() == 1 + len(deltas)


class TestLowStockCrossingBackAndForth:
    """Gap in `test_low_stock_event_fires_once_on_crossing`: that test only
    crosses DOWN once and confirms no re-fire while STAYING below. It never
    proves the event re-arms after a RECOVERY back above threshold — the
    genuinely revealing case for whether the crossing check
    (`StockTxnResult.crossed_into_low_stock`) is computed fresh from each
    txn's own `previous_quantity`/`new_quantity` (correct, per
    `apps.stock.services`) versus gated by some stateful "already notified"
    flag on the item (a plausible-looking but wrong alternative
    implementation that this test would catch: it would never re-fire on
    the second crossing below).
    """

    def test_event_refires_after_recovery_and_re_crossing(
        self, client, django_capture_on_commit_callbacks
    ):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        stock_item = StockItemFactory(tenant=tenant, reorder_threshold=5)
        _seed_ledger(stock_item, quantity=10, actor=admin)

        received: list[dict] = []

        def _handler(sender, **kwargs):
            received.append(kwargs)

        low_stock_crossed.connect(_handler)
        try:
            login_resp = client.post(
                "/api/v1/auth/login",
                {
                    "tenant": tenant.slug,
                    "email": admin.email,
                    "password": DEFAULT_TEST_PASSWORD,
                },
                content_type="application/json",
            )
            assert login_resp.status_code == 200, login_resp.content

            def _txn(delta: int, reason: str):
                with django_capture_on_commit_callbacks(execute=True):
                    resp = client.post(
                        f"/api/v1/stock/{stock_item.id}/txn/",
                        data=json.dumps({"delta": delta, "reason": reason}),
                        content_type="application/json",
                    )
                assert resp.status_code == 201, resp.content
                return resp

            # 1st crossing: 10 -> 4 (below threshold 5). Fires.
            _txn(-6, "consume")
            assert len(received) == 1
            assert (received[0]["previous_quantity"], received[0]["new_quantity"]) == (10, 4)

            # Recovery: 4 -> 20 (comfortably above). No event for
            # "recovering" (docs/data-model.md / apps.stock.services: only
            # a DOWNWARD crossing is a `low_stock` event) — must NOT fire.
            _txn(16, "receive")
            assert len(received) == 1  # unchanged

            # Small moves that stay on the healthy side: still no event.
            _txn(-1, "consume")  # 20 -> 19, still above threshold
            assert len(received) == 1

            # 2nd crossing: 19 -> 3 (below threshold again). MUST re-fire —
            # this is the assertion a stateful "already notified" flag would
            # fail.
            _txn(-16, "consume")
            assert len(received) == 2
            assert (received[1]["previous_quantity"], received[1]["new_quantity"]) == (19, 3)

            # Staying below (3 -> 2): must NOT re-fire a third time.
            _txn(-1, "consume")
            assert len(received) == 2
        finally:
            low_stock_crossed.disconnect(_handler)
