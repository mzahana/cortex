"""Celery task(s) for the notifications app (T5.1, `docs/architecture.md`
§6: "every send is a Celery task with retry/backoff").

`config/celery.py` autodiscovers `tasks.py` modules across every app in
`INSTALLED_APPS` (`app.autodiscover_tasks()`), so registering
`apps.notifications` there (this task) is the only wiring this module
needs -- no explicit import anywhere else.

## Retry/backoff policy

Exponential backoff (`retry_backoff=True`, capped at `retry_backoff_max`
seconds, `retry_jitter=True` to avoid a thundering herd if many sends fail
at once -- e.g. a Brevo outage) up to `max_retries` attempts total. On the
LAST allowed attempt, a transient failure is NOT retried again -- the
`EmailLog` row is marked `failed` and the task returns normally (it does
NOT re-raise), so a permanently-failing send shows up as a `failed`
`EmailLog` row, not as a Celery task marked `FAILURE` that some other layer
would need to notice separately. This is deliberately checked BEFORE
calling `self.retry(...)` (rather than relying on Celery's own
`MaxRetriesExceededError` propagation), so the exact "give up and log"
point is explicit and doesn't depend on retry-exhaustion internals.
"""

from __future__ import annotations

from typing import Any, Optional

from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.db import models
from django.utils import timezone

from apps.tenancy.context import tenant_context

from .models import EmailLog
from .providers import EmailSendError, get_email_provider


@shared_task(
    bind=True,
    name="apps.notifications.send_transactional_email",
    max_retries=5,
    retry_backoff=1,
    retry_backoff_max=600,
    retry_jitter=True,
)
def send_transactional_email(
    self,
    *,
    email_log_id: int,
    tenant_id: int,
    template_id: str,
    to: str,
    params: dict[str, Any],
    tags: Optional[list[str]] = None,
) -> None:
    """Send one templated email through the configured `EmailProvider` and
    update the matching `EmailLog` row. Never called synchronously from a
    request -- always enqueued via `apps.notifications.services.
    enqueue_transactional_email` (`.delay(...)`, after the enqueuing
    transaction commits)."""
    with tenant_context(tenant_id):
        try:
            log = EmailLog.all_objects.get(pk=email_log_id, tenant_id=tenant_id)
        except EmailLog.DoesNotExist:  # pragma: no cover - defensive only
            return

        provider = get_email_provider()
        try:
            result = provider.send_transactional(
                template_id=template_id, to=to, params=params, tags=tags
            )
        except EmailSendError as exc:
            if self.request.retries >= self.max_retries:
                # Retries exhausted: log the permanent failure and stop --
                # do not re-raise (see module docstring).
                log.status = EmailLog.Status.FAILED
                log.error = str(exc)
                log.save(update_fields=["status", "error", "updated_at"])
                return
            raise self.retry(exc=exc) from exc

        log.status = EmailLog.Status.SENT
        log.provider_message_id = result.provider_message_id
        log.error = ""
        log.save(update_fields=["status", "provider_message_id", "error", "updated_at"])


# ---------------------------------------------------------------------------
# T5.2 beat scans (`config/celery.py`'s `beat_schedule`, `docs/features.md`
# F9's "overdue reminder"/"low-stock alert" event types).
#
# Both scans loop over EVERY tenant (there is no single tenant context for a
# periodic task -- unlike an HTTP request or a signal fired from one), each
# entering `tenant_context(tenant.id)` explicitly (`apps.tenancy.context`
# docstring: "Celery tasks that already know the tenant ... use
# `tenant_context()` explicitly") so tenant-scoped queries and RLS both see
# the right tenant, one at a time -- and so a bug in one tenant's data never
# leaks a query into another tenant's rows (same fail-closed posture as
# every other tenant-scoped code path in this codebase).
#
# **Throttle mechanism (flagged assumption, `config/settings/base.py`'s
# `NOTIFICATION_OVERDUE_REMINDER_THROTTLE_SECONDS` /
# `..._LOW_STOCK_REMINDER_THROTTLE_SECONDS` docstring):** a `cache.add(key,
# ..., timeout=window)` guard on the SAME Redis instance already used for
# cache/sessions (`docs/architecture.md` "Redis does triple duty") -- `add()`
# is an atomic "set only if not already present" (`SET NX EX` under
# `django_redis`), so two beat ticks (or, in principle, two overlapping beat
# workers) racing on the same item can never both win and double-send. No
# schema change needed (an `EmailLog` row has no per-entity id column to key
# a DB-level throttle off -- see this task's own docstring in the exit
# criterria) -- reusing the existing Redis cache is the schema-free option,
# and keeps `apps/notifications` migrations untouched per this task's scope.
# ---------------------------------------------------------------------------


def _overdue_reminder_cache_key(checkout_id: int) -> str:
    return f"notif:overdue_reminder:checkout:{checkout_id}"


def _low_stock_reminder_cache_key(stock_item_id: int) -> str:
    return f"notif:low_stock_reminder:stock_item:{stock_item_id}"


@shared_task(name="apps.notifications.scan_overdue_checkouts")
def scan_overdue_checkouts() -> int:
    """Beat scan (F9 "overdue reminder"): every OPEN, past-due `Checkout`
    (`checked_in_at IS NULL AND due_at < now()` -- the exact
    `Checkout.is_overdue` predicate, backed by the
    `reservations_checkout_overdue_partial` index, `apps.reservations.
    checkout` module docstring) gets at most one reminder per throttle
    window, sent to its holder.

    Returns the number of reminders actually enqueued (tests/observability).
    """
    from apps.reservations.models import Checkout
    from apps.tenancy.models import Tenant

    from .services import enqueue_transactional_email

    window = settings.NOTIFICATION_OVERDUE_REMINDER_THROTTLE_SECONDS
    now = timezone.now()
    enqueued = 0

    for tenant in Tenant.objects.all():
        with tenant_context(tenant.id):
            overdue = Checkout.objects.filter(
                checked_in_at__isnull=True, due_at__lt=now
            ).select_related("asset", "user")
            for checkout in overdue:
                if checkout.user is None or not checkout.user.is_active:
                    continue
                cache_key = _overdue_reminder_cache_key(checkout.id)
                if not cache.add(cache_key, True, timeout=window):
                    continue  # already reminded within the throttle window
                enqueue_transactional_email(
                    tenant_id=tenant.id,
                    event_type="overdue_reminder",
                    template_id="overdue-reminder",
                    to=checkout.user.email,
                    params={
                        "checkout_id": checkout.id,
                        "asset_id": checkout.asset_id,
                        "due_at": checkout.due_at.isoformat(),
                    },
                    user=checkout.user,
                    optional=True,
                )
                enqueued += 1

    return enqueued


@shared_task(name="apps.notifications.scan_low_stock")
def scan_low_stock() -> int:
    """Beat scan (F9 "low-stock alert"): every `StockItem` currently AT-OR-
    BELOW its reorder threshold (`quantity_on_hand <= reorder_threshold` --
    backed by the `stock_stock_item` low-stock partial index,
    `apps.stock.models.StockItem.Meta` / T2.3 migration) gets at most one
    reminder per throttle window, sent to whoever can approve a reorder for
    that item's project.

    This is a periodic RE-CHECK/reminder for an item that REMAINS low --
    distinct from `apps.stock.signals.low_stock_crossed`
    (`apps.notifications.receivers.notify_low_stock_crossed`), which already
    fires immediately, once, on the crossing edge. An item that has been low
    for days without being restocked keeps getting this reminder (one per
    window) even though the one-time crossing-edge email already fired and
    will not fire again for the same item until it's restocked above
    threshold and crosses again.

    Returns the number of reminders actually enqueued (tests/observability).
    """
    from apps.rbac.permission_keys import REORDER_APPROVE
    from apps.stock.models import StockItem
    from apps.tenancy.models import Tenant

    from .recipients import users_with_permission_in_project_scope
    from .services import enqueue_transactional_email

    window = settings.NOTIFICATION_LOW_STOCK_REMINDER_THROTTLE_SECONDS
    enqueued = 0

    for tenant in Tenant.objects.all():
        with tenant_context(tenant.id):
            low_stock_items = StockItem.objects.filter(
                quantity_on_hand__lte=models.F("reorder_threshold")
            ).select_related("asset")
            for stock_item in low_stock_items:
                cache_key = _low_stock_reminder_cache_key(stock_item.id)
                if not cache.add(cache_key, True, timeout=window):
                    continue  # already reminded within the throttle window
                approvers = users_with_permission_in_project_scope(
                    REORDER_APPROVE, stock_item.asset.project_id
                )
                for approver in approvers:
                    enqueue_transactional_email(
                        tenant_id=tenant.id,
                        event_type="low_stock_alert",
                        template_id="low-stock-alert",
                        to=approver.email,
                        params={
                            "stock_item_id": stock_item.id,
                            "asset_id": stock_item.asset_id,
                            "quantity_on_hand": stock_item.quantity_on_hand,
                            "reorder_threshold": stock_item.reorder_threshold,
                        },
                        user=approver,
                        optional=True,
                    )
                enqueued += 1

    return enqueued
