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
