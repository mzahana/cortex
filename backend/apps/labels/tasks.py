"""Celery task for label PDF generation (T4.5). `config/celery.py`
autodiscovers `tasks.py` modules across every app in `INSTALLED_APPS`
(`app.autodiscover_tasks()`), so registering `apps.labels` there is the only
wiring this module needs — no explicit import anywhere else, same convention
`apps.notifications.tasks`'s module docstring documents.

Never dispatched synchronously from the view — always via `.delay(...)`
inside `transaction.on_commit(...)` in `apps.labels.api.LabelGenerateView`,
matching the notifications app's dispatch convention (see that module's
docstring: "Never called synchronously from a request").
"""

from __future__ import annotations

from celery import shared_task

from apps.assets.models import Asset
from apps.jobs.models import Job
from apps.tenancy.context import tenant_context

from .rendering import render_labels_pdf
from .services import resolve_label_data, save_label_pdf
from .templates import SHEET_TEMPLATES


@shared_task(
    bind=True,
    name="apps.labels.generate_label_pdf",
    max_retries=0,  # rendering is pure CPU work, no transient external
    # dependency to retry against (unlike `apps.notifications.tasks`'s
    # network-bound email send) -- a failure here is deterministic (bad
    # data/template) and retrying it would just fail again identically.
)
def generate_label_pdf(
    self, *, job_id: str, tenant_id: int, asset_ids: list[int], template_key: str
) -> None:
    """Render one Avery-sheet PDF for `asset_ids` (already tenant + RBAC
    scope-validated by the view BEFORE this was ever enqueued — this task
    trusts its own `asset_ids` input, same trust boundary
    `apps.notifications.tasks.send_transactional_email` has for its
    caller-resolved `email_log_id`) and update the matching `Job` row.

    Order of `asset_ids` is preserved on the sheet (the order the caller's
    `POST /labels/generate` request listed them in, after de-duplication —
    see `apps.labels.serializers.LabelGenerateRequestSerializer`).
    """
    with tenant_context(tenant_id):
        try:
            job = Job.objects.get(pk=job_id, tenant_id=tenant_id)
        except Job.DoesNotExist:  # pragma: no cover - defensive only
            return

        job.mark_running()

        try:
            template = SHEET_TEMPLATES[template_key]

            assets_by_id = {
                asset.id: asset
                for asset in Asset.objects.filter(id__in=asset_ids).select_related(
                    "category", "location"
                )
            }
            # Preserve the requested order; an asset id that vanished
            # between the request and the task running (e.g. retired via a
            # hard-delete path that doesn't exist yet, or a race with
            # another request) is simply skipped rather than failing the
            # whole job.
            ordered_assets = [assets_by_id[i] for i in asset_ids if i in assets_by_id]

            if not ordered_assets:
                job.mark_failed(error="None of the requested assets could be found.")
                return

            items = resolve_label_data(ordered_assets)
            pdf_bytes = render_labels_pdf(items, template)

            storage_key, filename = save_label_pdf(
                tenant_id=tenant_id, job_id=job.id, pdf_bytes=pdf_bytes
            )
            job.mark_succeeded(
                result_key=storage_key,
                result_filename=filename,
                result_content_type="application/pdf",
            )
        except Exception as exc:  # noqa: BLE001 - deliberately broad, see below
            # Any failure (bad template key, WeasyPrint/segno error, storage
            # write failure, ...) lands the job on FAILED with a short
            # message rather than leaving it stuck on RUNNING forever or
            # propagating a Celery `FAILURE` state some other layer would
            # need to separately notice — identical "give up and log"
            # posture to `apps.notifications.tasks.send_transactional_email`
            # on its last retry. `max_retries=0` above means this always
            # executes on the first and only attempt.
            job.mark_failed(error=str(exc)[:2000])
