"""Celery task(s) for the reservations app.

`config/celery.py` autodiscovers `tasks.py` modules across every app in
`INSTALLED_APPS` (`app.autodiscover_tasks()`), so registering
`apps.reservations` there (this task) is the only wiring this module needs --
no explicit import anywhere else.

## `expire_stale_reservations` (bug fix)

`Reservation.Status.EXPIRED` has existed on the model since T3.1, but until
this task nothing ever set it -- a `pending`/`approved` reservation whose
window quietly passed without being cancelled, rejected, or converted to a
checkout stayed `pending`/`approved` forever, wrongly continuing to
participate in `Reservation.ACTIVE_STATUSES` and the `0002` GiST exclusion
constraint's WHERE clause, blocking its own (already-past) window from ever
being re-booked.

This beat scan sweeps every `Reservation` with `status IN (pending,
approved)` and `end_at < now()` to `expired` per tenant, dropping it out of
`ACTIVE_STATUSES` and freeing the window. `fulfilled` reservations are
deliberately excluded (an asset that's still physically checked out stays
blocking until `perform_checkin` completes it, `apps.reservations.checkout`
module docstring) -- and terminal statuses (`rejected`/`cancelled`/
`completed`/already-`expired`) are untouched, there's nothing to do.

**Tenant scoping (same established pattern as
`apps.notifications.tasks.scan_overdue_checkouts`/`scan_low_stock`):** there
is no single tenant context for a periodic task (unlike an HTTP request or a
signal fired from one), so this loops over every `Tenant`, entering
`tenant_context(tenant.id)` explicitly for each one -- tenant-scoped queries
and RLS both see the right tenant, one at a time, and a bug in one tenant's
data can never leak a query into another tenant's rows.
"""

from __future__ import annotations

from celery import shared_task
from django.utils import timezone

from apps.tenancy.context import tenant_context

from .models import Reservation


@shared_task(name="apps.reservations.expire_stale_reservations")
def expire_stale_reservations() -> int:
    """Beat scan: every `pending`/`approved` reservation whose `end_at` has
    passed is swept to `expired`, per tenant. Returns the number of
    reservations actually expired (tests/observability)."""
    from apps.tenancy.models import Tenant

    now = timezone.now()
    expired_count = 0

    for tenant in Tenant.objects.all():
        with tenant_context(tenant.id):
            stale = Reservation.objects.filter(
                status__in=(Reservation.Status.PENDING, Reservation.Status.APPROVED),
                end_at__lt=now,
            )
            # `.update()` bypasses `auto_now`, so `updated_at` is set
            # explicitly here to the same `now` used for the filter.
            expired_count += stale.update(status=Reservation.Status.EXPIRED, updated_at=now)

    return expired_count
