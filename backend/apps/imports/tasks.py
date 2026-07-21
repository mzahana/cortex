"""Celery tasks for the bulk importer (T6.1). Autodiscovered by
`config/celery.py` (`app.autodiscover_tasks()`) via `apps.imports` being in
`INSTALLED_APPS` — same convention as `apps.labels.tasks`/
`apps.notifications.tasks`. Never dispatched synchronously — always via
`.delay(...)` inside `transaction.on_commit(...)` in `apps.imports.api`, so
a task never starts against an `ImportJob`/`Job` row the creating
transaction hasn't actually committed yet.

Each task opens the stored source file exactly ONCE (`default_storage.
open(...)`) and hands it straight to `apps.imports.services.build_report`/
`commit_import_rows`, which resolve the column mapping against the file's
own header row as part of that single streaming pass — see
`apps.imports.services.resolve_import_rows`'s docstring for why this
avoids a separate "peek the header row, then re-open/seek" step.
"""

from __future__ import annotations

from celery import shared_task
from django.core.files.storage import default_storage

from apps.dashboard.cache import invalidate_tenant_dashboard
from apps.tenancy.context import tenant_context
from apps.tenancy.models import Tenant

from .models import ImportJob
from .services import build_report, commit_import_rows

# `Job.mark_succeeded` requires `result_key`/`result_filename`/
# `result_content_type` (T4.5's shape: it always represents a downloadable
# blob) — imports have no such blob (the report lives on `ImportJob.report`
# instead, per that model's own docstring on why it isn't squeezed into
# `Job`), so every call below passes empty strings for all three. The
# client never reads `download_url` for an import job (it's `None`
# whenever `result_key` is empty, see `apps.jobs.serializers.
# JobSerializer.get_download_url`) — it polls `GET /jobs/{id}` purely for
# `status`/`error`, then fetches the real payload from
# `GET /api/v1/imports/{id}`.
_NO_RESULT_BLOB = {"result_key": "", "result_filename": "", "result_content_type": ""}


@shared_task(
    bind=True,
    name="apps.imports.run_dry_run",
    max_retries=0,  # deterministic (bad file/mapping/data) — a retry would fail identically.
)
def run_dry_run(self, *, import_job_id: int, tenant_id: int, mapping_override: dict) -> None:
    with tenant_context(tenant_id):
        try:
            import_job = ImportJob.objects.select_related("dry_run_job").get(pk=import_job_id)
        except ImportJob.DoesNotExist:  # pragma: no cover - defensive only
            return

        job = import_job.dry_run_job
        if job is not None:
            job.mark_running()
        import_job.status = ImportJob.Status.DRY_RUN_RUNNING
        import_job.save(update_fields=["status", "updated_at"])

        try:
            tenant = Tenant.objects.get(pk=tenant_id)
            with default_storage.open(import_job.source_storage_key, "rb") as fileobj:
                report = build_report(
                    tenant=tenant,
                    source_stream=fileobj,
                    filename=import_job.source_filename,
                    mapping_override=mapping_override,
                )

            import_job.mapping = report["resolved_mapping"]
            import_job.report = report
            import_job.status = ImportJob.Status.DRY_RUN_SUCCEEDED
            import_job.save(update_fields=["mapping", "report", "status", "updated_at"])

            if job is not None:
                job.mark_succeeded(**_NO_RESULT_BLOB)
        except Exception as exc:  # noqa: BLE001 - see apps.labels.tasks for this posture
            import_job.status = ImportJob.Status.DRY_RUN_FAILED
            import_job.save(update_fields=["status", "updated_at"])
            if job is not None:
                job.mark_failed(error=str(exc)[:2000])


@shared_task(
    bind=True,
    name="apps.imports.run_commit",
    max_retries=0,
)
def run_commit(self, *, import_job_id: int, tenant_id: int, mapping_override: dict) -> None:
    with tenant_context(tenant_id):
        try:
            import_job = ImportJob.objects.select_related("commit_job").get(pk=import_job_id)
        except ImportJob.DoesNotExist:  # pragma: no cover - defensive only
            return

        job = import_job.commit_job
        if job is not None:
            job.mark_running()
        import_job.status = ImportJob.Status.COMMITTING
        import_job.save(update_fields=["status", "updated_at"])

        try:
            tenant = Tenant.objects.get(pk=tenant_id)
            # An explicit override (client re-mapped at commit time) wins;
            # otherwise re-use the already-confirmed mapping from the last
            # dry run (see `apps.imports.api.ImportCommitView`).
            override = mapping_override or import_job.mapping
            with default_storage.open(import_job.source_storage_key, "rb") as fileobj:
                created_ids, report = commit_import_rows(
                    tenant=tenant,
                    source_stream=fileobj,
                    filename=import_job.source_filename,
                    mapping_override=override,
                )

            import_job.report = report
            import_job.mapping = report["resolved_mapping"]
            if created_ids:
                import_job.status = ImportJob.Status.COMMITTED
                import_job.created_asset_ids = created_ids
                import_job.save(
                    update_fields=[
                        "report",
                        "mapping",
                        "status",
                        "created_asset_ids",
                        "updated_at",
                    ]
                )
                if job is not None:
                    job.mark_succeeded(**_NO_RESULT_BLOB)
                invalidate_tenant_dashboard(tenant_id)
            else:
                import_job.status = ImportJob.Status.COMMIT_FAILED
                import_job.save(update_fields=["report", "mapping", "status", "updated_at"])
                if job is not None:
                    job.mark_failed(
                        error=(
                            f"{report['invalid_count']} row(s) failed validation; "
                            "no assets were created (all-or-nothing commit)."
                        )
                    )
        except Exception as exc:  # noqa: BLE001
            import_job.status = ImportJob.Status.COMMIT_FAILED
            import_job.save(update_fields=["status", "updated_at"])
            if job is not None:
                job.mark_failed(error=str(exc)[:2000])
