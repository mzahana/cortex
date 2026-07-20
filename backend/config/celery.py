"""Celery application (T0.3): wired to the Redis broker/result backend
(`CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND`, see config/settings/base.py),
with task autodiscovery across `apps/*` and a beat schedule.

`worker`/`beat` run this same app (see docker-compose.yml commands); anything
slow (emails, PDFs, imports, scans) becomes a task under an `apps/*/tasks.py`
module as those apps land — requests never block on them
(docs/architecture.md §4).
"""

import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

app = Celery("cortex")
# CELERY_* keys in Django settings configure the app (broker, result backend,
# serializers, time limits — see config/settings/base.py).
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

app.conf.beat_schedule = {
    # Placeholder heartbeat proving beat -> broker -> worker end-to-end.
    "celery-ping-heartbeat": {
        "task": "config.celery.ping",
        "schedule": crontab(minute="*/15"),
    },
    # T5.2 (docs/tasks/M5-notifications-audit-dashboard.md, docs/features.md
    # F9): hourly re-checks. Each scan is itself throttled per-item to at
    # most one reminder per `NOTIFICATION_*_REMINDER_THROTTLE_SECONDS` window
    # (`apps.notifications.tasks`), so running the scan more often than that
    # window merely re-checks sooner -- it never means more email is sent
    # than the throttle allows.
    "notifications-scan-overdue-checkouts": {
        "task": "apps.notifications.scan_overdue_checkouts",
        "schedule": crontab(minute=0),
    },
    "notifications-scan-low-stock": {
        "task": "apps.notifications.scan_low_stock",
        "schedule": crontab(minute=0),
    },
}


@app.task(name="config.celery.ping")
def ping() -> str:
    """Trivial task proving the broker/worker round-trip end-to-end."""
    return "pong"
