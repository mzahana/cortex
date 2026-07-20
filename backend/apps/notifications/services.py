"""Business-logic entry point for sending a templated transactional email
(T5.1). `apps.notifications.tasks.send_transactional_email` is the ONLY
thing that ever calls an `EmailProvider`; this module is the ONLY thing
domain-event handlers (T5.2 wires the real M2/M3 events -- `low_stock_
crossed`, `reservation_confirmed`, `approval_request`, `approval_decision`
-- to this) should call to actually trigger a send.

`enqueue_transactional_email` itself never sends anything and never blocks
on the network -- it writes one `EmailLog` row (status `queued`) and
schedules the Celery task via `transaction.on_commit` (same pattern as
`apps.stock.services._emit_low_stock_event_on_commit` /
`apps.reservations.services._emit_reservation_confirmed_on_commit`): the
task is only enqueued once the enqueuing transaction actually commits, so a
rolled-back caller never triggers a real send.
"""

from __future__ import annotations

from typing import Any, Optional

from django.conf import settings
from django.db import transaction

from .models import EmailLog, NotificationPref
from .tasks import send_transactional_email


def enqueue_transactional_email(
    *,
    tenant_id: int,
    event_type: str,
    template_id: str,
    to: str,
    params: dict[str, Any],
    user=None,
    optional: bool = True,
    tags: Optional[list[str]] = None,
) -> Optional[int]:
    """Enqueue one templated email for `event_type`.

    - `optional=True` (the default): an OPTIONAL event, gated by the
      recipient's `NotificationPref` (`docs/data-model.md`). If `user` holds
      a `NotificationPref` row for `event_type` with `email_enabled=False`,
      NOTHING is sent -- no `EmailLog` row is written at all (a caller can
      tell "skipped by preference" apart from "sent"/"failed" by the
      absence of a row for this event/recipient) and this returns `None`.
      A user with no explicit `NotificationPref` row defaults to enabled
      (opt-out, not opt-in -- matches `docs/rbac.md`'s "notify.self" being
      granted to every role by default).
    - `optional=False`: a security-critical/transactional event that always
      sends regardless of preference (none identified yet at T5.1 -- see
      `apps.notifications.models.NotificationPref` docstring).

    Returns the new `EmailLog.id`, or `None` if the send was skipped by
    preference.
    """
    if optional and user is not None:
        pref = (
            NotificationPref.all_objects.filter(
                tenant_id=tenant_id, user=user, event_type=event_type
            )
            .only("email_enabled")
            .first()
        )
        if pref is not None and not pref.email_enabled:
            return None

    log = EmailLog.all_objects.create(
        tenant_id=tenant_id,
        user=user,
        recipient=to,
        event_type=event_type,
        provider=settings.NOTIFICATION_EMAIL_PROVIDER.rsplit(".", 1)[-1],
        status=EmailLog.Status.QUEUED,
    )

    def _enqueue() -> None:
        send_transactional_email.delay(
            email_log_id=log.id,
            tenant_id=tenant_id,
            template_id=template_id,
            to=to,
            params=params,
            tags=tags,
        )

    transaction.on_commit(_enqueue)
    return log.id
