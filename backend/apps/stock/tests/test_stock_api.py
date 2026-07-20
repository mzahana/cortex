"""T2.2 — Stock endpoints: scope-aware listing + low-stock filter, the
atomic/concurrency-safe ledger-apply path (`POST /stock/{id}/txn`), the
`low_stock` crossing-edge event, reason-scoped RBAC (403s), audit coverage,
the reorder-request workflow (create/approve/cancel), and cross-tenant 404s
(add-endpoint golden path checklist).
"""

from __future__ import annotations

import json
import threading

import pytest
from django.db import connection

from apps.audit.models import AuditLog
from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
    ProjectFactory,
    StockItemFactory,
    TenantFactory,
    UserFactory,
    add_project_membership,
    upgrade_tenant_wide_role,
)
from apps.rbac.models import Membership
from apps.rbac.permission_keys import ROLE_ADMIN, ROLE_PROJECT_LEAD, ROLE_VIEWER
from apps.stock.models import ReorderRequest, StockTxn
from apps.stock.services import apply_stock_txn
from apps.stock.signals import low_stock_crossed
from apps.tenancy.context import tenant_context

pytestmark = pytest.mark.django_db


def _login(client, tenant, user):
    response = client.post(
        "/api/v1/auth/login",
        {"tenant": tenant.slug, "email": user.email, "password": DEFAULT_TEST_PASSWORD},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return response


def _seed_ledger(stock_item, *, quantity: int, actor=None) -> None:
    """Give `stock_item` an initial ledger row + reconciled quantity via a
    RECEIVE txn (not by poking `quantity_on_hand` directly — every quantity
    in these tests should come from the ledger, same rule the endpoint
    itself enforces)."""
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


class TestStockList:
    def test_list_is_tenant_and_scope_aware(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant)

        pool_item = StockItemFactory(tenant=tenant)
        project_item = StockItemFactory(tenant=tenant)
        with tenant_context(tenant.id):
            project_item.asset.project = project
            project_item.asset.save(update_fields=["project"])

        other_tenant = TenantFactory()
        StockItemFactory(tenant=other_tenant)

        lead = UserFactory(tenant=tenant)
        # Remove the auto-assigned tenant-wide Member membership (which would
        # otherwise ALSO grant tenant-wide asset.view) so this user's ONLY
        # membership is the project-scoped ProjectLead grant below — the
        # "pure ProjectLead" scenario (docs/rbac.md §1) this test exists to
        # prove, matching `apps.assets.tests.test_assets_list_search_filters`.
        Membership.all_objects.filter(user=lead, project__isnull=True).delete()
        add_project_membership(lead, project, ROLE_PROJECT_LEAD)

        _login(client, tenant, admin)
        response = client.get("/api/v1/stock/")
        assert response.status_code == 200, response.content
        ids = {row["id"] for row in response.json()["results"]}
        assert ids == {pool_item.id, project_item.id}
        client.post("/api/v1/auth/logout")

        _login(client, tenant, lead)
        response = client.get("/api/v1/stock/")
        assert response.status_code == 200, response.content
        ids = {row["id"] for row in response.json()["results"]}
        # A pure ProjectLead (project-scoped `asset.view` only) sees ONLY
        # their project's stock — never the general pool, never another
        # project (docs/rbac.md §1).
        assert ids == {project_item.id}

    def test_low_stock_filter(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)

        low_item = StockItemFactory(tenant=tenant, reorder_threshold=5)
        _seed_ledger(low_item, quantity=3)
        healthy_item = StockItemFactory(tenant=tenant, reorder_threshold=5)
        _seed_ledger(healthy_item, quantity=50)

        _login(client, tenant, admin)
        response = client.get("/api/v1/stock/?low_stock=true")
        assert response.status_code == 200, response.content
        ids = {row["id"] for row in response.json()["results"]}
        assert ids == {low_item.id}

    def test_cross_tenant_stock_id_404(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)

        other_tenant = TenantFactory()
        other_item = StockItemFactory(tenant=other_tenant)

        _login(client, tenant, admin)
        response = client.get(f"/api/v1/stock/{other_item.id}/")
        assert response.status_code == 404


class TestStockTxn:
    def test_receive_increments_quantity_via_ledger(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        stock_item = StockItemFactory(tenant=tenant)

        _login(client, tenant, admin)
        response = client.post(
            f"/api/v1/stock/{stock_item.id}/txn/",
            data=json.dumps({"delta": 10, "reason": "receive", "ref": "PO-1"}),
            content_type="application/json",
        )
        assert response.status_code == 201, response.content
        body = response.json()
        assert body["stock_item"]["quantity_on_hand"] == 10
        assert body["txn"]["delta"] == 10
        assert body["txn"]["reason"] == "receive"

        with tenant_context(tenant.id):
            stock_item.refresh_from_db()
            assert stock_item.quantity_on_hand == 10
            assert stock_item.quantity_on_hand == stock_item.recompute_quantity_on_hand()
            assert StockTxn.objects.filter(stock_item=stock_item).count() == 1

    def test_consume_decrements_quantity_via_ledger(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        member = UserFactory(tenant=tenant)  # default Member membership
        stock_item = StockItemFactory(tenant=tenant)
        _seed_ledger(stock_item, quantity=10, actor=admin)

        _login(client, tenant, member)
        response = client.post(
            f"/api/v1/stock/{stock_item.id}/txn/",
            data=json.dumps({"delta": -4, "reason": "consume"}),
            content_type="application/json",
        )
        assert response.status_code == 201, response.content
        body = response.json()
        assert body["stock_item"]["quantity_on_hand"] == 6

        with tenant_context(tenant.id):
            stock_item.refresh_from_db()
            assert stock_item.quantity_on_hand == 6
            assert stock_item.quantity_on_hand == stock_item.recompute_quantity_on_hand()
            assert StockTxn.objects.filter(stock_item=stock_item).count() == 2

    def test_consume_cannot_drive_quantity_negative(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        stock_item = StockItemFactory(tenant=tenant)
        _seed_ledger(stock_item, quantity=2, actor=admin)

        _login(client, tenant, admin)
        response = client.post(
            f"/api/v1/stock/{stock_item.id}/txn/",
            data=json.dumps({"delta": -5, "reason": "consume"}),
            content_type="application/json",
        )
        assert response.status_code == 400, response.content

        with tenant_context(tenant.id):
            stock_item.refresh_from_db()
            assert stock_item.quantity_on_hand == 2  # unchanged
            # The rejected txn's row never landed (whole atomic block rolled back).
            assert StockTxn.objects.filter(stock_item=stock_item).count() == 1

    def test_correction_is_a_new_row_not_an_edit(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        stock_item = StockItemFactory(tenant=tenant)
        _seed_ledger(stock_item, quantity=10, actor=admin)

        _login(client, tenant, admin)
        response = client.post(
            f"/api/v1/stock/{stock_item.id}/txn/",
            data=json.dumps({"delta": -2, "reason": "correction", "ref": "miscount"}),
            content_type="application/json",
        )
        assert response.status_code == 201, response.content

        with tenant_context(tenant.id):
            rows = list(StockTxn.objects.filter(stock_item=stock_item).order_by("created_at"))
            assert len(rows) == 2
            assert rows[0].reason == StockTxn.Reason.RECEIVE  # original untouched
            assert rows[0].delta == 10
            assert rows[1].reason == StockTxn.Reason.CORRECTION
            stock_item.refresh_from_db()
            assert stock_item.quantity_on_hand == 8

    def test_unauthorized_adjust_returns_403_for_plain_member(self, client):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)  # default Member: stock.consume, not stock.adjust
        stock_item = StockItemFactory(tenant=tenant)
        _seed_ledger(stock_item, quantity=10)

        _login(client, tenant, member)
        response = client.post(
            f"/api/v1/stock/{stock_item.id}/txn/",
            data=json.dumps({"delta": 5, "reason": "receive"}),
            content_type="application/json",
        )
        assert response.status_code == 403, response.content

        with tenant_context(tenant.id):
            stock_item.refresh_from_db()
            assert stock_item.quantity_on_hand == 10  # unchanged

    def test_viewer_cannot_consume(self, client):
        tenant = TenantFactory()
        viewer = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(viewer, ROLE_VIEWER)
        stock_item = StockItemFactory(tenant=tenant)
        _seed_ledger(stock_item, quantity=10)

        _login(client, tenant, viewer)
        response = client.post(
            f"/api/v1/stock/{stock_item.id}/txn/",
            data=json.dumps({"delta": -1, "reason": "consume"}),
            content_type="application/json",
        )
        assert response.status_code == 403, response.content

    def test_project_lead_can_adjust_only_their_own_project_stock(self, client):
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        other_project = ProjectFactory(tenant=tenant)
        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, project, ROLE_PROJECT_LEAD)

        own_item = StockItemFactory(tenant=tenant)
        other_item = StockItemFactory(tenant=tenant)
        with tenant_context(tenant.id):
            own_item.asset.project = project
            own_item.asset.save(update_fields=["project"])
            other_item.asset.project = other_project
            other_item.asset.save(update_fields=["project"])

        _login(client, tenant, lead)

        ok_response = client.post(
            f"/api/v1/stock/{own_item.id}/txn/",
            data=json.dumps({"delta": 5, "reason": "receive"}),
            content_type="application/json",
        )
        assert ok_response.status_code == 201, ok_response.content

        denied_response = client.post(
            f"/api/v1/stock/{other_item.id}/txn/",
            data=json.dumps({"delta": 5, "reason": "receive"}),
            content_type="application/json",
        )
        assert denied_response.status_code == 403, denied_response.content

    def test_stock_adjust_reason_writes_audit_log(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        stock_item = StockItemFactory(tenant=tenant)

        _login(client, tenant, admin)
        response = client.post(
            f"/api/v1/stock/{stock_item.id}/txn/",
            data=json.dumps({"delta": 10, "reason": "receive"}),
            content_type="application/json",
        )
        assert response.status_code == 201, response.content

        entries = AuditLog.all_objects.filter(
            tenant_id=tenant.id, entity_type="stock_item", entity_id=str(stock_item.id)
        )
        assert entries.count() == 1
        entry = entries.first()
        assert entry is not None
        assert entry.action == "stock.adjust"
        assert entry.actor_id == admin.id
        assert entry.before == {"quantity_on_hand": 0}
        assert entry.after is not None
        assert entry.after["quantity_on_hand"] == 10

    def test_consume_does_not_write_audit_log(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        stock_item = StockItemFactory(tenant=tenant)
        _seed_ledger(stock_item, quantity=10, actor=admin)

        _login(client, tenant, admin)
        response = client.post(
            f"/api/v1/stock/{stock_item.id}/txn/",
            data=json.dumps({"delta": -1, "reason": "consume"}),
            content_type="application/json",
        )
        assert response.status_code == 201, response.content

        entries = AuditLog.all_objects.filter(
            tenant_id=tenant.id, entity_type="stock_item", entity_id=str(stock_item.id)
        )
        assert entries.count() == 0

    def test_low_stock_event_fires_once_on_crossing(
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
            _login(client, tenant, admin)

            # 10 -> 4: crosses the threshold (5) -- fires exactly once.
            with django_capture_on_commit_callbacks(execute=True):
                response = client.post(
                    f"/api/v1/stock/{stock_item.id}/txn/",
                    data=json.dumps({"delta": -6, "reason": "consume"}),
                    content_type="application/json",
                )
            assert response.status_code == 201, response.content
            assert len(received) == 1
            assert received[0]["stock_item_id"] == stock_item.id
            assert received[0]["previous_quantity"] == 10
            assert received[0]["new_quantity"] == 4
            assert received[0]["reorder_threshold"] == 5

            # 4 -> 3: still under threshold -- ALREADY low, must NOT re-fire.
            with django_capture_on_commit_callbacks(execute=True):
                response = client.post(
                    f"/api/v1/stock/{stock_item.id}/txn/",
                    data=json.dumps({"delta": -1, "reason": "consume"}),
                    content_type="application/json",
                )
            assert response.status_code == 201, response.content
            assert len(received) == 1  # unchanged
        finally:
            low_stock_crossed.disconnect(_handler)

    def test_low_stock_event_does_not_fire_on_a_rolled_back_txn(
        self, client, django_capture_on_commit_callbacks
    ):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        stock_item = StockItemFactory(tenant=tenant, reorder_threshold=5)
        _seed_ledger(stock_item, quantity=2, actor=admin)  # already below threshold

        received: list[dict] = []

        def _handler(sender, **kwargs):
            received.append(kwargs)

        low_stock_crossed.connect(_handler)
        try:
            _login(client, tenant, admin)
            with django_capture_on_commit_callbacks(execute=True):
                response = client.post(
                    f"/api/v1/stock/{stock_item.id}/txn/",
                    data=json.dumps({"delta": -100, "reason": "consume"}),
                    content_type="application/json",
                )
            assert response.status_code == 400, response.content
            assert received == []
        finally:
            low_stock_crossed.disconnect(_handler)

    def test_concurrent_consume_txns_serialize_and_reconcile(self, transactional_db):
        """Concurrency safety: `apply_stock_txn`'s `select_for_update()` on
        the `StockItem` row serializes concurrent writers against the SAME
        item so the ledger sum and `quantity_on_hand` can never lose an
        update, even when several txns are applied at (as close to) the
        same wall-clock time as native threads allow.
        """
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        stock_item = StockItemFactory(tenant=tenant, reorder_threshold=0)
        _seed_ledger(stock_item, quantity=20, actor=admin)

        thread_count = 5
        barrier = threading.Barrier(thread_count)
        errors: list[BaseException] = []

        def worker():
            try:
                barrier.wait(timeout=5)
                with tenant_context(tenant.id):
                    apply_stock_txn(
                        stock_item=stock_item,
                        delta=-1,
                        reason=StockTxn.Reason.CONSUME.value,
                        ref="",
                        actor=admin,
                    )
            except BaseException as exc:  # pragma: no cover - surfaced via `errors`
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=worker) for _ in range(thread_count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert not errors, errors
        with tenant_context(tenant.id):
            stock_item.refresh_from_db()
            assert stock_item.quantity_on_hand == 20 - thread_count
            assert stock_item.quantity_on_hand == stock_item.recompute_quantity_on_hand()
            assert StockTxn.objects.filter(stock_item=stock_item).count() == 1 + thread_count


class TestReorderRequests:
    def test_member_can_create_reorder_request(self, client):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        stock_item = StockItemFactory(tenant=tenant)

        _login(client, tenant, member)
        response = client.post(
            "/api/v1/reorder-requests/",
            data=json.dumps({"stock_item": stock_item.id, "quantity": 10, "note": "low"}),
            content_type="application/json",
        )
        assert response.status_code == 201, response.content
        body = response.json()
        assert body["status"] == "open"
        assert body["requested_by"] == member.id

    def test_viewer_cannot_create_reorder_request(self, client):
        tenant = TenantFactory()
        viewer = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(viewer, ROLE_VIEWER)
        stock_item = StockItemFactory(tenant=tenant)

        _login(client, tenant, viewer)
        response = client.post(
            "/api/v1/reorder-requests/",
            data=json.dumps({"stock_item": stock_item.id, "quantity": 10}),
            content_type="application/json",
        )
        assert response.status_code == 403, response.content

    def test_approve_requires_reorder_approve_and_writes_audit_log(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        member = UserFactory(tenant=tenant)
        stock_item = StockItemFactory(tenant=tenant)
        with tenant_context(tenant.id):
            reorder = ReorderRequest.objects.create(
                tenant=tenant, stock_item=stock_item, requested_by=member, quantity=10
            )

        _login(client, tenant, member)
        denied = client.patch(
            f"/api/v1/reorder-requests/{reorder.id}/",
            data=json.dumps({"status": "approved"}),
            content_type="application/json",
        )
        assert denied.status_code == 403, denied.content
        client.post("/api/v1/auth/logout")

        _login(client, tenant, admin)
        response = client.patch(
            f"/api/v1/reorder-requests/{reorder.id}/",
            data=json.dumps({"status": "approved"}),
            content_type="application/json",
        )
        assert response.status_code == 200, response.content
        body = response.json()
        assert body["status"] == "approved"
        assert body["approved_by"] == admin.id
        assert body["decided_at"] is not None

        entries = AuditLog.all_objects.filter(
            tenant_id=tenant.id, entity_type="reorder_request", entity_id=str(reorder.id)
        )
        assert entries.count() == 1
        entry = entries.first()
        assert entry is not None
        assert entry.action == "reorder.approve"

    def test_ordered_and_received_transitions_also_write_audit_log(self, client):
        """Code-review follow-up (T2.2): `approved -> ordered` and
        `ordered -> received` are ALSO gated by `reorder.approve`
        (`apps.stock.permissions.APPROVER_ONLY_TRANSITIONS`), so
        `docs/rbac.md` §5 ("every exercise of `reorder.approve` is audited")
        requires an AuditLog entry for each of the three approve-gated
        transitions — not just the initial `open -> approved` decision.
        """
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        stock_item = StockItemFactory(tenant=tenant)
        with tenant_context(tenant.id):
            reorder = ReorderRequest.objects.create(
                tenant=tenant, stock_item=stock_item, quantity=10
            )

        _login(client, tenant, admin)
        for target_status in ("approved", "ordered", "received"):
            response = client.patch(
                f"/api/v1/reorder-requests/{reorder.id}/",
                data=json.dumps({"status": target_status}),
                content_type="application/json",
            )
            assert response.status_code == 200, response.content
            assert response.json()["status"] == target_status

        entries = AuditLog.all_objects.filter(
            tenant_id=tenant.id, entity_type="reorder_request", entity_id=str(reorder.id)
        ).order_by("created_at")
        assert [e.action for e in entries] == [
            "reorder.approve",
            "reorder.approve",
            "reorder.approve",
        ]
        after_statuses = [e.after["status"] for e in entries if e.after is not None]
        assert after_statuses == ["approved", "ordered", "received"]

    def test_cancel_transition_is_not_audited(self, client):
        """`cancelled` is reachable by the original requester (not only an
        approver) and is deliberately excluded from the mandatory-audit set
        — a self-cancel is not an exercise of `reorder.approve`.
        """
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        stock_item = StockItemFactory(tenant=tenant)
        with tenant_context(tenant.id):
            reorder = ReorderRequest.objects.create(
                tenant=tenant, stock_item=stock_item, requested_by=member, quantity=10
            )

        _login(client, tenant, member)
        response = client.patch(
            f"/api/v1/reorder-requests/{reorder.id}/",
            data=json.dumps({"status": "cancelled"}),
            content_type="application/json",
        )
        assert response.status_code == 200, response.content

        entries = AuditLog.all_objects.filter(
            tenant_id=tenant.id, entity_type="reorder_request", entity_id=str(reorder.id)
        )
        assert entries.count() == 0

    def test_terminal_request_rejects_plain_field_edits(self, client):
        """Code-review follow-up (T2.2): a `received`/`cancelled` request is
        immutable outright — a plain field edit (no `status` in the body)
        against one is rejected with a 4xx, not silently applied.
        """
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        stock_item = StockItemFactory(tenant=tenant)

        with tenant_context(tenant.id):
            received_request = ReorderRequest.objects.create(
                tenant=tenant,
                stock_item=stock_item,
                requested_by=admin,
                quantity=10,
                status=ReorderRequest.Status.RECEIVED,
            )
            cancelled_request = ReorderRequest.objects.create(
                tenant=tenant,
                stock_item=stock_item,
                requested_by=admin,
                quantity=10,
                status=ReorderRequest.Status.CANCELLED,
            )
            open_request = ReorderRequest.objects.create(
                tenant=tenant, stock_item=stock_item, requested_by=admin, quantity=10
            )

        _login(client, tenant, admin)

        for terminal_request in (received_request, cancelled_request):
            response = client.patch(
                f"/api/v1/reorder-requests/{terminal_request.id}/",
                data=json.dumps({"quantity": 99, "note": "trying to sneak an edit in"}),
                content_type="application/json",
            )
            assert 400 <= response.status_code < 500, response.content
            with tenant_context(tenant.id):
                terminal_request.refresh_from_db()
                assert terminal_request.quantity == 10  # unchanged

        # A non-terminal (`open`) request is still editable.
        ok_response = client.patch(
            f"/api/v1/reorder-requests/{open_request.id}/",
            data=json.dumps({"quantity": 42}),
            content_type="application/json",
        )
        assert ok_response.status_code == 200, ok_response.content
        assert ok_response.json()["quantity"] == 42

    def test_requester_can_cancel_own_open_request(self, client):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        stock_item = StockItemFactory(tenant=tenant)

        _login(client, tenant, member)
        create_response = client.post(
            "/api/v1/reorder-requests/",
            data=json.dumps({"stock_item": stock_item.id, "quantity": 3}),
            content_type="application/json",
        )
        reorder_id = create_response.json()["id"]

        response = client.patch(
            f"/api/v1/reorder-requests/{reorder_id}/",
            data=json.dumps({"status": "cancelled"}),
            content_type="application/json",
        )
        assert response.status_code == 200, response.content
        assert response.json()["status"] == "cancelled"

    def test_invalid_transition_rejected_with_400(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        stock_item = StockItemFactory(tenant=tenant)
        with tenant_context(tenant.id):
            reorder = ReorderRequest.objects.create(
                tenant=tenant, stock_item=stock_item, requested_by=admin, quantity=10
            )

        _login(client, tenant, admin)
        response = client.patch(
            f"/api/v1/reorder-requests/{reorder.id}/",
            data=json.dumps({"status": "received"}),  # open -> received is not a valid jump
            content_type="application/json",
        )
        assert response.status_code == 400, response.content

    def test_cross_tenant_reorder_request_id_404(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)

        other_tenant = TenantFactory()
        other_item = StockItemFactory(tenant=other_tenant)
        with tenant_context(other_tenant.id):
            other_reorder = ReorderRequest.objects.create(
                tenant=other_tenant, stock_item=other_item, quantity=1
            )

        _login(client, tenant, admin)
        response = client.get(f"/api/v1/reorder-requests/{other_reorder.id}/")
        assert response.status_code == 404
