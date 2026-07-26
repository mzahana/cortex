"""Celery task for the M7 project audit report PDF (Slice 3,
`docs/tasks/M7-project-grants.md`). `config/celery.py` autodiscovers
`tasks.py` modules across every app in `INSTALLED_APPS`
(`app.autodiscover_tasks()`), so registering `apps.projects` there is the
only wiring this module needs — no explicit import anywhere else, same
convention `apps.labels.tasks`'s own module docstring documents.

Never dispatched synchronously from the view — always via `.delay(...)`
inside `transaction.on_commit(...)` in `apps.projects.api.ProjectViewSet.
report`, identical dispatch convention to `apps.labels.api.
LabelGenerateView`.
"""

from __future__ import annotations

from celery import shared_task

from apps.jobs.models import Job
from apps.tenancy.context import tenant_context

from .models import Project
from .report import render_project_report_pdf
from .services import resolve_project_report_data, save_project_report_pdf


@shared_task(
    bind=True,
    name="apps.projects.generate_project_report_pdf",
    max_retries=0,  # rendering is pure CPU work reading already-committed DB
    # rows/storage bytes -- no transient external dependency to retry
    # against, same reasoning `apps.labels.tasks.generate_label_pdf` gives
    # for its own `max_retries=0`.
)
def generate_project_report_pdf(
    self,
    *,
    job_id: str,
    tenant_id: int,
    project_id: int,
    include_invoice_scans: bool = False,
    include_project_documents: bool = False,
) -> None:
    """Render one project's audit report PDF and update the matching `Job`
    row. `project_id` was already tenant + RBAC scope-validated by
    `apps.projects.api.ProjectViewSet.report` (via `self.get_object()`,
    `expense.view` scoped to this exact project) BEFORE this was ever
    enqueued — this task trusts its own `project_id` input, same trust
    boundary `apps.labels.tasks.generate_label_pdf` has for its
    caller-resolved `asset_ids`.

    `include_invoice_scans` and `include_project_documents` are the
    caller-supplied opt-ins (both default `False`) threaded straight through
    to `resolve_project_report_data` — see that function's docstring for
    what turning each one on does.
    """
    with tenant_context(tenant_id):
        try:
            job = Job.objects.get(pk=job_id, tenant_id=tenant_id)
        except Job.DoesNotExist:  # pragma: no cover - defensive only
            return

        job.mark_running()

        try:
            try:
                project = Project.objects.select_related("tenant", "lead_user").get(pk=project_id)
            except Project.DoesNotExist:
                # Project deleted/moved between the request and the task
                # running -- fail the job rather than crash silently, same
                # "give up cleanly" posture `apps.labels.tasks` uses when
                # every requested asset has vanished.
                job.mark_failed(error="The project no longer exists.")
                return

            data = resolve_project_report_data(
                project,
                include_invoice_scans=include_invoice_scans,
                include_project_documents=include_project_documents,
            )
            pdf_bytes = render_project_report_pdf(data)

            storage_key, filename = save_project_report_pdf(
                tenant_id=tenant_id, job_id=job.id, pdf_bytes=pdf_bytes
            )
            job.mark_succeeded(
                result_key=storage_key,
                result_filename=filename,
                result_content_type="application/pdf",
            )
        except Exception as exc:  # noqa: BLE001 - deliberately broad, see below
            # Any failure (bad data, WeasyPrint error, storage write
            # failure, ...) lands the job on FAILED with a short message
            # rather than leaving it stuck on RUNNING forever -- identical
            # "give up and log" posture to `apps.labels.tasks.
            # generate_label_pdf`. `max_retries=0` above means this always
            # executes on the first and only attempt.
            job.mark_failed(error=str(exc)[:2000])
