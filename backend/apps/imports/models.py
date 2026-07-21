"""`ImportJob` (T6.1, `docs/tasks/M6-import-export-deploy.md`): the
import-specific state a CSV/Excel bulk import needs beyond what the generic
`apps.jobs.Job` row already tracks.

**Why a separate model instead of stuffing everything onto `Job`.** `Job`
(T4.5) is deliberately shaped for a single-blob-in/single-blob-out async
action (label PDF generation: `params` in, `result_key` out) — see that
model's own docstring. An import is a richer, multi-step flow: one uploaded
file feeds a **dry-run** validation pass (produces a column-mapping +
per-row report the client reviews) and then, separately, a **commit** pass
against the SAME file (confirmed mapping, creates assets). Two `Job` rows
(`dry_run_job`, `commit_job`) provide the generic "queued/running/succeeded/
failed" polling contract (`GET /api/v1/jobs/{id}`, unchanged, reused as-is);
`ImportJob` holds everything else: the uploaded file's storage key, the
confirmed column mapping, and the latest validation report — none of which
fits `Job.params`/`result_key`'s single-blob shape.

**Column-mapping/schema assumption (Q8, `docs/risks.md` — flagged, no
representative spreadsheet was provided).** See `apps.imports.services`'
module docstring for the exact expected-column schema this importer is
built against.

Tenant-owned -> `TenantScopedModel` (T0.4's fail-closed manager); RLS lands
in `0002_rls_policies.py`, same house convention as every other tenant table
(`CLAUDE.md`).
"""

from __future__ import annotations

from django.db import models

from apps.tenancy.models import TenantScopedModel


class ImportJob(TenantScopedModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        DRY_RUN_RUNNING = "dry_run_running", "Dry-run running"
        DRY_RUN_SUCCEEDED = "dry_run_succeeded", "Dry-run succeeded"
        DRY_RUN_FAILED = "dry_run_failed", "Dry-run failed"
        COMMITTING = "committing", "Committing"
        COMMITTED = "committed", "Committed"
        COMMIT_FAILED = "commit_failed", "Commit failed"

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)

    # The two `Job` rows driving this import's async work (T4.5's generic
    # poll contract, `GET /api/v1/jobs/{id}`) — `commit_job` is null until
    # `POST /imports/{id}/commit` is called. `SET_NULL`: an import's own
    # history/report must survive even if its `Job` row were ever pruned.
    dry_run_job = models.OneToOneField(
        "jobs.Job", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    commit_job = models.OneToOneField(
        "jobs.Job", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    # Uploaded source file: bytes live on the storage volume (django-storages,
    # same as `apps.assets.models.Attachment.storage_key`/`apps.labels`'s
    # rendered PDFs) — only the key + metadata are ever persisted here.
    source_filename = models.CharField(max_length=255)
    source_storage_key = models.CharField(max_length=500)
    source_content_type = models.CharField(max_length=127, blank=True, default="")

    # The column mapping actually used for the most recent dry-run/commit
    # pass: `{"<spreadsheet header>": "<target field>"}` — see
    # `apps.imports.services.CORE_TARGETS` for the fixed target vocabulary
    # (anything else is treated as a per-category custom-field column).
    # Starts as the caller-supplied override merged over the auto-detected
    # default (`apps.imports.services.default_column_mapping`).
    mapping = models.JSONField(default=dict, blank=True)

    # The latest validation report (dry-run OR the commit pass's own
    # re-validation — see `apps.imports.services.build_report`): per-row
    # resolved values + errors, plus valid/invalid counts. `null` until the
    # first dry-run task has actually run.
    report = models.JSONField(null=True, blank=True)

    # Populated once `status == COMMITTED`: the ids of the `Asset` rows this
    # import created, in row order — a convenience the client can use to
    # jump straight to the new assets without re-deriving them from the
    # report.
    created_asset_ids = models.JSONField(default=list, blank=True)

    created_by = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="import_jobs",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "imports_import_job"
        indexes = [models.Index(fields=["tenant", "created_by"])]
        ordering = ["-created_at"]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"ImportJob({self.id}, {self.status})"
