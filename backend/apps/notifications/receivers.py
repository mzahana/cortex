"""T5.2 — wires the M2/M3 domain events to `enqueue_transactional_email`
(`docs/tasks/M5-notifications-audit-dashboard.md` T5.2, `docs/features.md`
F9). Connected via `NotificationsConfig.ready()` (same pattern as
`apps.rbac.apps.RbacConfig.ready()` importing `apps.rbac.signals`).

Four of F9's five event types are wired here as `@receiver`s on the
already-existing M2/M3 signals (`apps.stock.signals.low_stock_crossed`,
`apps.reservations.signals.{approval_request,approval_decision,
reservation_confirmed}`). The fifth (overdue reminder) has no signal -- it is
driven entirely by the `scan_overdue_checkouts` beat task in
`apps.notifications.tasks`, which calls `enqueue_transactional_email`
directly.

**Every receiver here runs from `transaction.on_commit`, sent via
`send_robust`** (see each signal's own module docstring) -- by the time any
of these fire, the tenant's `CurrentTenantMiddleware` has already entered
`tenant_context()` for the request that triggered the underlying commit, so
plain tenant-scoped queries (`Asset.objects`, `users_with_permission_in_
project_scope`) are safe to use directly, with no extra `tenant_context()`
entry needed here (unlike the beat tasks in `tasks.py`, which scan across
every tenant and must enter it themselves per-tenant).

A receiver raising is swallowed by `send_robust` and never breaks the
request/task that emitted the signal -- but each receiver below still tries
to fail safe internally (e.g. skip a signal referencing a since-deleted
asset) rather than relying solely on that backstop.
"""

from __future__ import annotations

from django.dispatch import receiver

from apps.reservations.signals import approval_decision, approval_request, reservation_confirmed
from apps.stock.signals import low_stock_crossed

from .recipients import users_with_permission_in_project_scope
from .services import enqueue_transactional_email

# Event-type keys stored on `EmailLog.event_type` / used to key
# `NotificationPref` rows (docs/data-model.md). These are the F9 event
# catalog (docs/features.md F9): reservation confirmation, approval
# request/decision, overdue reminder, low-stock alert.
EVENT_RESERVATION_CONFIRMED = "reservation_confirmed"
EVENT_APPROVAL_REQUEST = "approval_request"
EVENT_APPROVAL_DECISION = "approval_decision"
EVENT_OVERDUE_REMINDER = "overdue_reminder"
EVENT_LOW_STOCK_ALERT = "low_stock_alert"


def _get_user(user_id):
    """Best-effort tenant-scoped `User` lookup for a signal's `user_id` --
    `None` if it no longer resolves (defensive only; users are never
    hard-deleted, docs/data-model.md, so this should not normally miss)."""
    from apps.accounts.models import User

    return User.objects.filter(pk=user_id).first()


def _get_asset(asset_id):
    from apps.assets.models import Asset

    return Asset.objects.filter(pk=asset_id).first()


@receiver(reservation_confirmed)
def notify_reservation_confirmed(sender, **kwargs) -> None:
    """Notify the requester their booking is confirmed (F9 "reservation
    confirmation")."""
    user = _get_user(kwargs["user_id"])
    if user is None:
        return
    enqueue_transactional_email(
        tenant_id=kwargs["tenant_id"],
        event_type=EVENT_RESERVATION_CONFIRMED,
        template_id="reservation-confirmed",
        to=user.email,
        params={
            "reservation_id": kwargs["reservation_id"],
            "asset_id": kwargs["asset_id"],
            "start_at": kwargs["start_at"].isoformat(),
            "end_at": kwargs["end_at"].isoformat(),
        },
        user=user,
        optional=True,
    )


@receiver(approval_request)
def notify_approval_request(sender, **kwargs) -> None:
    """Notify whoever can decide this reservation: every ACTIVE user holding
    `reservation.approve` tenant-wide, plus the asset's project's
    ProjectLead(s) if it's project-scoped (`docs/rbac.md` §4)."""
    from apps.rbac.permission_keys import RESERVATION_APPROVE

    asset = _get_asset(kwargs["asset_id"])
    if asset is None:
        return
    approvers = users_with_permission_in_project_scope(RESERVATION_APPROVE, asset.project_id)
    for approver in approvers:
        enqueue_transactional_email(
            tenant_id=kwargs["tenant_id"],
            event_type=EVENT_APPROVAL_REQUEST,
            template_id="approval-request",
            to=approver.email,
            params={
                "reservation_id": kwargs["reservation_id"],
                "asset_id": kwargs["asset_id"],
                "requester_id": kwargs["user_id"],
                "start_at": kwargs["start_at"].isoformat(),
                "end_at": kwargs["end_at"].isoformat(),
            },
            user=approver,
            optional=True,
        )


@receiver(approval_decision)
def notify_approval_decision(sender, **kwargs) -> None:
    """Notify the original requester of the approve/reject outcome."""
    user = _get_user(kwargs["user_id"])
    if user is None:
        return
    enqueue_transactional_email(
        tenant_id=kwargs["tenant_id"],
        event_type=EVENT_APPROVAL_DECISION,
        template_id="approval-decision",
        to=user.email,
        params={
            "reservation_id": kwargs["reservation_id"],
            "asset_id": kwargs["asset_id"],
            "approved": kwargs["approved"],
            "note": kwargs["note"],
        },
        user=user,
        optional=True,
    )


@receiver(low_stock_crossed)
def notify_low_stock_crossed(sender, **kwargs) -> None:
    """Notify whoever can approve a reorder for this item's project the
    INSTANT it crosses into low stock (the edge-triggered event; the
    periodic re-check/reminder for an item that STAYS low is
    `apps.notifications.tasks.scan_low_stock`, throttled separately)."""
    from apps.rbac.permission_keys import REORDER_APPROVE

    asset = _get_asset(kwargs["asset_id"])
    if asset is None:
        return
    approvers = users_with_permission_in_project_scope(REORDER_APPROVE, asset.project_id)
    for approver in approvers:
        enqueue_transactional_email(
            tenant_id=kwargs["tenant_id"],
            event_type=EVENT_LOW_STOCK_ALERT,
            template_id="low-stock-alert",
            to=approver.email,
            params={
                "stock_item_id": kwargs["stock_item_id"],
                "asset_id": kwargs["asset_id"],
                "previous_quantity": kwargs["previous_quantity"],
                "new_quantity": kwargs["new_quantity"],
                "reorder_threshold": kwargs["reorder_threshold"],
            },
            user=approver,
            optional=True,
        )
