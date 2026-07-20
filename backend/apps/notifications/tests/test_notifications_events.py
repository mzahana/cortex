"""T5.2 acceptance (`docs/tasks/M5-notifications-audit-dashboard.md`,
`docs/features.md` F9): the five event types are wired to `enqueue_
transactional_email` and the two beat scans produce reminders, throttled.

Each of the five event types results in exactly one `EmailLog` row (or the
correctly-gated zero, if `NotificationPref` disables it):
1. `reservation_confirmed` -- `TestReservationConfirmedEvent`.
2. `approval_request` -- `TestApprovalRequestEvent`.
3. `approval_decision` -- `TestApprovalDecisionEvent`.
4. `overdue_reminder` (beat scan, no signal) -- `TestOverdueReminderScan`.
5. `low_stock_alert` (crossing-edge signal AND the beat re-check/reminder
   scan) -- `TestLowStockAlertEvent`, `TestLowStockReminderScan`.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone

from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
    AssetFactory,
    CategoryFactory,
    ProjectFactory,
    StockItemFactory,
    TenantFactory,
    UserFactory,
    add_project_membership,
    upgrade_tenant_wide_role,
)
from apps.notifications.models import EmailLog, NotificationPref
from apps.notifications.tasks import scan_low_stock, scan_overdue_checkouts
from apps.rbac.permission_keys import ROLE_ADMIN, ROLE_PROJECT_LEAD
from apps.reservations.models import Checkout
from apps.reservations.services import approve_reservation, create_reservation
from apps.stock.models import StockTxn
from apps.stock.services import apply_stock_txn
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


def _window(hours_from_now: int = 1, duration_hours: int = 2):
    start = timezone.now() + timedelta(hours=hours_from_now)
    end = start + timedelta(hours=duration_hours)
    return start, end


class TestReservationConfirmedEvent:
    def test_auto_approved_reservation_notifies_requester(
        self, client, django_capture_on_commit_callbacks
    ):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        category = CategoryFactory(tenant=tenant, requires_approval=False)
        asset = AssetFactory(tenant=tenant, category=category)

        with tenant_context(tenant.id):
            with django_capture_on_commit_callbacks(execute=True):
                reservation = create_reservation(
                    tenant=tenant,
                    actor=member,
                    asset=asset,
                    start_at=_window()[0],
                    end_at=_window()[1],
                )

            assert reservation.status == "approved"
            logs = EmailLog.all_objects.filter(
                tenant_id=tenant.id, event_type="reservation_confirmed"
            )
            assert logs.count() == 1
            log = logs.get()
            assert log.recipient == member.email
            assert log.status == EmailLog.Status.SENT

    def test_disabled_pref_sends_nothing(self, django_capture_on_commit_callbacks):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        category = CategoryFactory(tenant=tenant, requires_approval=False)
        asset = AssetFactory(tenant=tenant, category=category)

        with tenant_context(tenant.id):
            NotificationPref.all_objects.create(
                tenant=tenant, user=member, event_type="reservation_confirmed", email_enabled=False
            )
            with django_capture_on_commit_callbacks(execute=True):
                create_reservation(
                    tenant=tenant,
                    actor=member,
                    asset=asset,
                    start_at=_window()[0],
                    end_at=_window()[1],
                )

            assert not EmailLog.all_objects.filter(
                tenant_id=tenant.id, event_type="reservation_confirmed"
            ).exists()


class TestApprovalRequestEvent:
    def test_pending_reservation_notifies_scoped_approvers(
        self, django_capture_on_commit_callbacks
    ):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant)
        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, project, ROLE_PROJECT_LEAD)
        other_lead = UserFactory(tenant=tenant)
        other_project = ProjectFactory(tenant=tenant)
        add_project_membership(other_lead, other_project, ROLE_PROJECT_LEAD)

        category = CategoryFactory(tenant=tenant, requires_approval=True)
        asset = AssetFactory(tenant=tenant, category=category, project=project)

        with tenant_context(tenant.id):
            with django_capture_on_commit_callbacks(execute=True):
                reservation = create_reservation(
                    tenant=tenant,
                    actor=member,
                    asset=asset,
                    start_at=_window()[0],
                    end_at=_window()[1],
                )

            assert reservation.status == "pending"
            logs = EmailLog.all_objects.filter(tenant_id=tenant.id, event_type="approval_request")
            recipients = {log.recipient for log in logs}
            # The tenant-wide Admin and the asset's OWN project's lead --
            # never the other project's lead (docs/rbac.md §1 scope rule).
            assert recipients == {admin.email, lead.email}


class TestApprovalDecisionEvent:
    def test_approve_notifies_the_requester(self, django_capture_on_commit_callbacks):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = CategoryFactory(tenant=tenant, requires_approval=True)
        asset = AssetFactory(tenant=tenant, category=category)

        with tenant_context(tenant.id):
            with django_capture_on_commit_callbacks(execute=True):
                reservation = create_reservation(
                    tenant=tenant,
                    actor=member,
                    asset=asset,
                    start_at=_window()[0],
                    end_at=_window()[1],
                )

            with django_capture_on_commit_callbacks(execute=True):
                approve_reservation(reservation=reservation, actor=admin)

            decision_logs = EmailLog.all_objects.filter(
                tenant_id=tenant.id, event_type="approval_decision"
            )
            assert decision_logs.count() == 1
            assert decision_logs.get().recipient == member.email

            # Approving ALSO fires reservation_confirmed (a distinct fact,
            # apps.reservations.signals module docstring) -- exactly one of
            # each, not a duplicate of either.
            confirmed_logs = EmailLog.all_objects.filter(
                tenant_id=tenant.id, event_type="reservation_confirmed"
            )
            assert confirmed_logs.count() == 1
            assert confirmed_logs.get().recipient == member.email


class TestOverdueReminderScan:
    def test_scan_notifies_holder_exactly_once_per_throttle_window(
        self, django_capture_on_commit_callbacks
    ):
        tenant = TenantFactory()
        holder = UserFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))

        with tenant_context(tenant.id):
            checked_out_at = timezone.now() - timedelta(days=3)
            due_at = timezone.now() - timedelta(days=1)
            checkout = Checkout.all_objects.create(
                tenant=tenant,
                asset=asset,
                user=holder,
                checked_out_at=checked_out_at,
                due_at=due_at,
            )

        cache.clear()
        with django_capture_on_commit_callbacks(execute=True):
            enqueued_first = scan_overdue_checkouts()
        assert enqueued_first == 1

        with tenant_context(tenant.id):
            logs = EmailLog.all_objects.filter(tenant_id=tenant.id, event_type="overdue_reminder")
            assert logs.count() == 1
            log = logs.get()
            assert log.recipient == holder.email
            assert log.status == EmailLog.Status.SENT

        # Running the scan again immediately (still well inside the default
        # 24h throttle window) must NOT enqueue a second reminder for the
        # SAME overdue checkout.
        with django_capture_on_commit_callbacks(execute=True):
            enqueued_second = scan_overdue_checkouts()
        assert enqueued_second == 0
        with tenant_context(tenant.id):
            assert (
                EmailLog.all_objects.filter(
                    tenant_id=tenant.id, event_type="overdue_reminder"
                ).count()
                == 1
            )

        with tenant_context(tenant.id):
            checkout.refresh_from_db()
            assert checkout.is_overdue

    def test_scan_ignores_checked_in_and_not_yet_due_items(self):
        tenant = TenantFactory()
        holder = UserFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        other_asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))

        with tenant_context(tenant.id):
            now = timezone.now()
            # Already checked in -- not open, must be ignored.
            Checkout.all_objects.create(
                tenant=tenant,
                asset=asset,
                user=holder,
                checked_out_at=now - timedelta(days=3),
                due_at=now - timedelta(days=1),
                checked_in_at=now - timedelta(hours=1),
            )
            # Open but not yet due -- must be ignored.
            Checkout.all_objects.create(
                tenant=tenant,
                asset=other_asset,
                user=holder,
                checked_out_at=now,
                due_at=now + timedelta(days=1),
            )

        cache.clear()
        assert scan_overdue_checkouts() == 0
        with tenant_context(tenant.id):
            assert not EmailLog.all_objects.filter(
                tenant_id=tenant.id, event_type="overdue_reminder"
            ).exists()

    def test_throttle_window_setting_gates_a_second_reminder(self):
        """Explicitly proves the throttle is time-window-based (not merely
        "never twice"): shrinking the window to 0 seconds via settings lets
        an immediate second scan send again."""
        tenant = TenantFactory()
        holder = UserFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))

        with tenant_context(tenant.id):
            now = timezone.now()
            Checkout.all_objects.create(
                tenant=tenant,
                asset=asset,
                user=holder,
                checked_out_at=now - timedelta(days=3),
                due_at=now - timedelta(days=1),
            )

        cache.clear()
        with override_settings(NOTIFICATION_OVERDUE_REMINDER_THROTTLE_SECONDS=0):
            assert scan_overdue_checkouts() == 1
            assert scan_overdue_checkouts() == 1  # window already elapsed (0s)


class TestLowStockAlertEvent:
    def test_crossing_the_threshold_notifies_scoped_reorder_approvers(
        self, django_capture_on_commit_callbacks
    ):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant)
        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, project, ROLE_PROJECT_LEAD)

        with tenant_context(tenant.id):
            stock_item = StockItemFactory(
                tenant=tenant, reorder_threshold=5, asset__project=project
            )
            with django_capture_on_commit_callbacks(execute=True):
                apply_stock_txn(
                    stock_item=stock_item,
                    delta=10,
                    reason=StockTxn.Reason.RECEIVE,
                    ref="seed",
                    actor=admin,
                )
            with django_capture_on_commit_callbacks(execute=True):
                apply_stock_txn(
                    stock_item=stock_item,
                    delta=-8,
                    reason=StockTxn.Reason.CONSUME,
                    ref="use",
                    actor=admin,
                )

            logs = EmailLog.all_objects.filter(tenant_id=tenant.id, event_type="low_stock_alert")
            recipients = {log.recipient for log in logs}
            assert recipients == {admin.email, lead.email}


class TestLowStockReminderScan:
    def test_scan_notifies_once_per_throttle_window_for_a_still_low_item(
        self, django_capture_on_commit_callbacks
    ):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)

        with tenant_context(tenant.id):
            # `quantity_on_hand` is set AT CREATE TIME (an INSERT), never via
            # a later `.save()` (an UPDATE) -- the T2.3 `stock_item_validate_
            # qty_bu` trigger only fires `BEFORE UPDATE`, requiring
            # `quantity_on_hand` to already equal the (empty, here) StockTxn
            # ledger sum; an insert with no prior ledger has nothing to
            # reconcile against yet, same as `apps.stock.perf_seed`'s "no
            # StockTxn seeded for this row" rows never being touched by an
            # UPDATE either.
            StockItemFactory(tenant=tenant, reorder_threshold=5, quantity_on_hand=2)

        cache.clear()
        with django_capture_on_commit_callbacks(execute=True):
            assert scan_low_stock() == 1
        with tenant_context(tenant.id):
            logs = EmailLog.all_objects.filter(tenant_id=tenant.id, event_type="low_stock_alert")
            assert logs.count() == 1
            log = logs.get()
            assert log.recipient == admin.email
            assert log.status == EmailLog.Status.SENT

        # A second scan inside the same throttle window must not re-notify.
        with django_capture_on_commit_callbacks(execute=True):
            assert scan_low_stock() == 0
        with tenant_context(tenant.id):
            assert (
                EmailLog.all_objects.filter(
                    tenant_id=tenant.id, event_type="low_stock_alert"
                ).count()
                == 1
            )

    def test_scan_ignores_items_above_threshold(self):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)

        with tenant_context(tenant.id):
            StockItemFactory(tenant=tenant, reorder_threshold=5, quantity_on_hand=50)

        cache.clear()
        assert scan_low_stock() == 0


class TestBeatScansDoNotBlockRequests:
    def test_beat_task_names_are_not_referenced_from_any_view_module(self):
        """Structural proof the two scans are independent periodic tasks,
        never called synchronously from a request path: neither task
        function is imported anywhere under `apps/*/api.py` or
        `apps/*/services.py` (the request-facing modules) -- their only
        caller is Celery beat, via `config/celery.py`'s `beat_schedule`."""
        import ast
        import pathlib

        backend_root = pathlib.Path(__file__).resolve().parents[3]
        request_facing = list(backend_root.glob("apps/*/api.py")) + list(
            backend_root.glob("apps/*/services.py")
        )
        for path in request_facing:
            source = path.read_text()
            tree = ast.parse(source)
            names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
            assert "scan_overdue_checkouts" not in names, path
            assert "scan_low_stock" not in names, path
