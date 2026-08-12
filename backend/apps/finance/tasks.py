"""Celery task for the expense pack PDF (M8 Phase 3 §6.1).

`config/celery.py` autodiscovers `tasks.py` across every app in
`INSTALLED_APPS`, so registering `apps.finance` there is all the wiring this
needs — same convention as `apps.projects.tasks`/`apps.labels.tasks`.

Never dispatched synchronously: `apps.finance.orders.OrderViewSet.pack`
dispatches via `.delay(...)` inside `transaction.on_commit(...)` so the task
can never run against a `Job` row that has not committed yet.
"""

from __future__ import annotations

from celery import shared_task

from apps.jobs.models import Job
from apps.tenancy.context import tenant_context

from .models import Payment
from .pack import render_order_pack_pdf
from .services import resolve_order_pack_data, save_order_pack_pdf


@shared_task(
    bind=True,
    name="apps.finance.generate_order_pack_pdf",
    # Pure CPU work over already-committed rows and storage bytes — there is
    # no transient external dependency worth retrying against, same reasoning
    # `apps.projects.tasks.generate_project_report_pdf` gives.
    max_retries=0,
)
def generate_order_pack_pdf(self, *, job_id: str, tenant_id: int, order_id: int) -> None:
    """Render one order's audit pack and attach it to `job_id`.

    **Everything runs inside `tenant_context`, including fetching the `Job`
    itself.** The worker connects as the non-superuser `cortex_app` role, which
    is subject to Row-Level Security, so a `Job` lookup made outside the tenant
    context matches zero rows — even through `all_objects`, which bypasses the
    tenant-scoped manager but not the database policy. An earlier cut fetched
    the job first and returned quietly on `DoesNotExist`: the task "succeeded"
    in 8ms having done nothing, the job sat on `queued` forever, and the UI
    eventually timed out with a generic error. Silent, and hard to see from
    either end. Same structure as `apps.projects.tasks` for exactly this reason.
    """
    with tenant_context(tenant_id):
        try:
            job = Job.objects.get(pk=job_id, tenant_id=tenant_id)
        except Job.DoesNotExist:  # pragma: no cover - defensive only
            return

        job.mark_running()

        try:
            try:
                order = (
                    Payment.objects.select_related("project", "tenant")
                    .prefetch_related(
                        "attachments",
                        "purchases__attachments",
                        "purchases__expenses__asset_links__asset__attachments",
                    )
                    .get(pk=order_id)
                )
            except Payment.DoesNotExist:
                # Deleted between enqueue and run — fail the job with a clear
                # message rather than leaving it running forever.
                job.mark_failed(error="The expense no longer exists.")
                return

            data = resolve_order_pack_data(
                order, generated_by=(job.created_by.email if job.created_by else "")
            )
            pdf_bytes = render_order_pack_pdf(data)
            storage_key, filename = save_order_pack_pdf(
                tenant_id=tenant_id, job_id=job.id, pdf_bytes=pdf_bytes, order=order
            )
            job.mark_succeeded(
                result_key=storage_key,
                result_filename=filename,
                result_content_type="application/pdf",
            )
        except Exception as exc:  # noqa: BLE001 - any render failure lands on the job
            job.mark_failed(error=str(exc)[:2000])


@shared_task(
    bind=True,
    name="apps.finance.generate_audit_checklist_pdf",
    max_retries=0,
)
def generate_audit_checklist_pdf(self, *, job_id: str, tenant_id: int, project_id: int) -> None:
    """Render the audit-readiness checklist for one project.

    Same tenant-context discipline as `generate_order_pack_pdf` above — see its
    docstring for why the `Job` lookup itself must be inside the context.
    """
    from apps.projects.models import Project

    from .checklist import render_checklist_pdf
    from .services import resolve_checklist_data, save_checklist_pdf

    with tenant_context(tenant_id):
        try:
            job = Job.objects.get(pk=job_id, tenant_id=tenant_id)
        except Job.DoesNotExist:  # pragma: no cover - defensive only
            return

        job.mark_running()
        try:
            try:
                project = Project.objects.select_related("tenant").get(pk=project_id)
            except Project.DoesNotExist:
                job.mark_failed(error="The project no longer exists.")
                return

            data = resolve_checklist_data(
                project, generated_by=(job.created_by.email if job.created_by else "")
            )
            pdf_bytes = render_checklist_pdf(data)
            storage_key, filename = save_checklist_pdf(
                tenant_id=tenant_id, job_id=job.id, pdf_bytes=pdf_bytes
            )
            job.mark_succeeded(
                result_key=storage_key,
                result_filename=filename,
                result_content_type="application/pdf",
            )
        except Exception as exc:  # noqa: BLE001
            job.mark_failed(error=str(exc)[:2000])
