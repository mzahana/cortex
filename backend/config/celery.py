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

app = Celery("lms")
# CELERY_* keys in Django settings configure the app (broker, result backend,
# serializers, time limits — see config/settings/base.py).
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

app.conf.beat_schedule = {
    # Placeholder heartbeat proving beat -> broker -> worker end-to-end; M2+
    # replaces/augments this with the real overdue/low-stock scan schedules.
    "celery-ping-heartbeat": {
        "task": "config.celery.ping",
        "schedule": crontab(minute="*/15"),
    },
}


@app.task(name="config.celery.ping")
def ping() -> str:
    """Trivial task proving the broker/worker round-trip end-to-end."""
    return "pong"
