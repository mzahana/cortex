"""Generic async-job tracking (T4.5, first landed for label PDF generation —
`docs/tasks/M4-mobile-scan-labels.md`'s note: "design it small and reusable
since M6 (import/export) will need the identical `/jobs/{id}` polling
pattern later, but don't over-build for M6 now").

`Job` is the one tenant-owned row a slow Celery task reports its progress
through: a request-time endpoint (e.g. `POST /api/v1/labels/generate`)
creates a `QUEUED` row and dispatches a task (never blocking the request,
CLAUDE.md); the task flips it to `RUNNING`, does the work, and lands on
`SUCCEEDED` (with `result_key` pointing at the output file on the SAME
storage backend/volume `apps.assets.models.Attachment` already uses) or
`FAILED` (with `error` set) — never left `RUNNING` forever on an uncaught
exception, see `apps.labels.tasks` for how that's guaranteed.

`TenantScopedModel` (T0.4): the RLS backstop for this table lands in this
same migration set (`0002_rls.py`) per house convention (`CLAUDE.md`:
"RLS ... in its own migration, same milestone as the table").

**RBAC on read is deliberately narrow (task instructions): `created_by` ==
the requesting user, tenant-scoped via `Job.objects`.** Not a scope-sharing
model — a job is a private, ephemeral polling handle for the request that
created it, not a shared team resource. See `apps.jobs.api.JobRetrieveView`.
"""

from __future__ import annotations

import uuid as uuid_lib

from django.db import models

from apps.tenancy.models import TenantScopedModel


class Job(TenantScopedModel):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    # UUID primary key (not the usual `BigAutoField`): a job id is handed to
    # the client in a URL (`GET /api/v1/jobs/{id}`) and polled repeatedly —
    # unguessable/non-enumerable, same reasoning as `Asset.qr_token`, though
    # here it doubles as the actual primary key rather than a separate
    # opaque field, since a `Job` has no other natural public identity.
    id = models.UUIDField(primary_key=True, default=uuid_lib.uuid4, editable=False)

    # e.g. "label_pdf" (T4.5); M6 import/export adds more values later —
    # deliberately a plain `CharField`, not a `TextChoices`, so a future
    # milestone's new job type is an additive string, not a migration.
    job_type = models.CharField(max_length=50, db_index=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.QUEUED)

    # Whatever the task needs to do the work / whatever the caller wants to
    # remember about the request (e.g. label job's asset ids + template key)
    # — opaque to this app, read/written only by the specific task/view that
    # created the job. Never used for RBAC/tenant scoping (that's `tenant`/
    # `created_by`, real columns).
    params = models.JSONField(default=dict, blank=True)

    # Populated on SUCCEEDED only: the storage key (relative path on the
    # storage backend/volume, same pattern as `apps.assets.models.Attachment.
    # storage_key` — the bytes never enter the DB) the client downloads via
    # a dedicated download endpoint.
    result_key = models.CharField(max_length=500, blank=True, default="")
    result_filename = models.CharField(max_length=255, blank=True, default="")
    result_content_type = models.CharField(max_length=127, blank=True, default="")

    # Populated on FAILED only: a short, user-facing message (never a raw
    # traceback — see `apps.labels.tasks` for what's actually written here).
    error = models.TextField(blank=True, default="")

    created_by = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="jobs",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "jobs_job"
        indexes = [
            models.Index(fields=["tenant", "created_by"]),
            models.Index(fields=["tenant", "job_type"]),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.job_type}:{self.id} ({self.status})"

    def mark_running(self) -> None:
        from django.utils import timezone

        self.status = self.Status.RUNNING
        self.started_at = timezone.now()
        self.save(update_fields=["status", "started_at", "updated_at"])

    def mark_succeeded(
        self, *, result_key: str, result_filename: str, result_content_type: str
    ) -> None:
        from django.utils import timezone

        self.status = self.Status.SUCCEEDED
        self.result_key = result_key
        self.result_filename = result_filename
        self.result_content_type = result_content_type
        self.finished_at = timezone.now()
        self.save(
            update_fields=[
                "status",
                "result_key",
                "result_filename",
                "result_content_type",
                "finished_at",
                "updated_at",
            ]
        )

    def mark_failed(self, *, error: str) -> None:
        from django.utils import timezone

        self.status = self.Status.FAILED
        self.error = error
        self.finished_at = timezone.now()
        self.save(update_fields=["status", "error", "finished_at", "updated_at"])
