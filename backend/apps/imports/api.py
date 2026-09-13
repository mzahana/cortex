"""`POST /api/v1/imports` (dry-run upload), `GET /api/v1/imports/{id}`
(poll the mapping/report), `POST /api/v1/imports/{id}/commit` (T6.1).

Tenant scoping (golden-path step 2): every queryset here is `ImportJob.
objects...`, the tenant-scoped manager, resolved per-request.

RBAC (golden-path step 3): `import.run` — Admin only, tenant-wide, no
ProjectLead scope (`apps.imports.permissions.ImportRunPermission`, see its
own docstring for exactly which rbac.md cell this enforces).

Async dispatch (golden-path step 4, CLAUDE.md "slow work runs in Celery"):
both the dry-run and commit Celery tasks are enqueued via
`transaction.on_commit(...)`, same convention as `apps.labels.api.
LabelGenerateView` — a task can never start against a `Job`/`ImportJob` row
the creating transaction hasn't actually committed yet.

Audit (golden-path step 5): a committed import creates `Asset` rows via the
SAME `apps.assets.services`/model path an ordinary `POST /api/v1/assets`
create does — and `docs/rbac.md` §5's mandatory-audit list does NOT include
`asset.create` (only `asset.retire` and friends), so — matching that
existing, deliberate behavior exactly — a committed import does not write
its own `AuditLog` entries either. See this task's own instructions:
"reuse that [audit] path, don't build a separate audit call" — there is no
audit call on the normal create path to reuse, so none is added here.
"""

from __future__ import annotations

from django.db import transaction
from rest_framework import generics, permissions, status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.errors import problem_response
from apps.jobs.models import Job

from .models import ImportJob
from .permissions import ImportRunPermission, missing_create_permissions
from .serializers import (
    ImportCommitRequestSerializer,
    ImportJobSerializer,
    ImportUploadRequestSerializer,
)
from .services import (
    DEFAULT_ON_DUPLICATE,
    ON_DUPLICATE_CHOICES,
    save_import_source_file,
)
from .tasks import run_commit, run_dry_run


def _create_missing_forbidden(denied: list[str]):
    """`create_missing` asked for a target this user can't create. A 403,
    not a 400: the request is well-formed, the caller just lacks
    `category.manage`/`location.manage`/`tenant.manage` (see
    `apps.imports.permissions.CREATE_MISSING_PERMISSION_KEYS`)."""
    return problem_response(
        status_code=status.HTTP_403_FORBIDDEN,
        title="Not allowed to create missing references",
        detail=(
            "You do not have permission to create new "
            f"{', '.join(denied)} records. Re-run the import without "
            "those in `create_missing`, or ask an admin to add them first."
        ),
    )


class ImportUploadView(APIView):
    permission_classes = [permissions.IsAuthenticated, ImportRunPermission]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        serializer = ImportUploadRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        uploaded_file = serializer.validated_data["file"]
        mapping_override: dict = serializer.validated_data.get("mapping") or {}
        create_missing: list[str] = serializer.validated_data.get("create_missing") or []
        on_duplicate: str = serializer.validated_data.get("on_duplicate") or DEFAULT_ON_DUPLICATE

        denied = missing_create_permissions(request.user, create_missing)
        if denied:
            return _create_missing_forbidden(denied)

        import_job = ImportJob.objects.create(
            tenant=request.user.tenant,
            source_filename=uploaded_file.name,
            source_content_type=getattr(uploaded_file, "content_type", "") or "",
            created_by=request.user,
        )
        # The storage key is namespaced by the `ImportJob`'s own id (see
        # `apps.imports.services.import_source_storage_key`), so the row
        # must exist first — same two-step "create row, then save the file
        # keyed by its id" ordering `apps.labels.services.save_label_pdf`
        # uses for a `Job` id.
        storage_key, filename, content_type = save_import_source_file(
            tenant_id=request.user.tenant_id,
            import_job_id=import_job.id,
            uploaded_file=uploaded_file,
        )
        job = Job.objects.create(
            tenant=request.user.tenant,
            job_type="import_dry_run",
            created_by=request.user,
        )
        import_job.source_storage_key = storage_key
        import_job.source_filename = filename
        import_job.source_content_type = content_type
        import_job.dry_run_job = job
        import_job.save(
            update_fields=[
                "source_storage_key",
                "source_filename",
                "source_content_type",
                "dry_run_job",
                "updated_at",
            ]
        )

        transaction.on_commit(
            lambda: run_dry_run.delay(
                import_job_id=import_job.id,
                tenant_id=import_job.tenant_id,
                mapping_override=mapping_override,
                create_missing=create_missing,
                on_duplicate=on_duplicate,
            )
        )

        return Response(ImportJobSerializer(import_job).data, status=status.HTTP_202_ACCEPTED)


def _import_job_queryset():
    return ImportJob.objects.select_related("dry_run_job", "commit_job")


class ImportDetailView(generics.RetrieveAPIView):
    """Poll the mapping/report for an import (dry-run OR the most recent
    commit's re-validation) — `GET /jobs/{dry_run_job.id | commit_job.id}`
    (T4.5, unchanged) is still what the client polls for a plain
    queued/running/succeeded/failed status; this endpoint is where the
    RICHER, import-specific state actually lives (see `apps.imports.models.
    ImportJob`'s docstring for why it isn't squeezed into `Job`).

    Deliberately NOT narrowed to `created_by=request.user` the way
    `apps.jobs.api.JobRetrieveView` is: an import is an Admin, tenant-wide
    bulk-inventory operation (`import.run` has no scoped/private-to-me
    reading in `docs/rbac.md` §3), so any Admin in the tenant may look up
    and commit an import a colleague started — tenant isolation (the
    `ImportJob.objects` manager) is still the only boundary.
    """

    serializer_class = ImportJobSerializer
    permission_classes = [permissions.IsAuthenticated, ImportRunPermission]
    lookup_field = "pk"
    lookup_url_kwarg = "import_id"

    def get_queryset(self):
        return _import_job_queryset()


class ImportCommitView(APIView):
    permission_classes = [permissions.IsAuthenticated, ImportRunPermission]

    def post(self, request, import_id: int):
        try:
            import_job = _import_job_queryset().get(pk=import_id)
        except ImportJob.DoesNotExist:
            return problem_response(
                status_code=status.HTTP_404_NOT_FOUND,
                title="Not found",
                detail="No import job with that id.",
            )

        if import_job.status not in (
            ImportJob.Status.DRY_RUN_SUCCEEDED,
            ImportJob.Status.COMMIT_FAILED,
        ):
            return problem_response(
                status_code=status.HTTP_409_CONFLICT,
                title="Import not ready to commit",
                detail=(
                    "Run a dry-run (POST /imports) and wait for it to succeed " "before committing."
                ),
            )

        serializer = ImportCommitRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        mapping_override: dict = serializer.validated_data.get("mapping") or {}
        # Same fallback shape as `mapping` (see the task): an explicit
        # request value wins, otherwise re-use whatever the last dry-run was
        # asked to create, so ticking the boxes, re-validating, then
        # committing doesn't silently drop the choice. Either way the
        # permission check below runs against what will ACTUALLY be created
        # — a colleague committing someone else's import is re-checked, not
        # trusted because the dry-run's author was allowed.
        create_missing: list[str] = serializer.validated_data.get("create_missing") or (
            (import_job.report or {}).get("create_missing") or []
        )

        # Same "explicit value wins, else re-use the dry-run's" fallback as
        # `mapping`/`create_missing`: the user picked how to handle
        # duplicates while looking at the dry-run report, and an empty
        # commit body must not silently revert that to the default.
        # Presence in `request.data`, not the serializer's default (which is
        # always populated and would defeat the fallback entirely). The
        # value itself is already validated by the `ChoiceField` above.
        on_duplicate: str = (
            serializer.validated_data["on_duplicate"]
            if "on_duplicate" in request.data
            else ((import_job.report or {}).get("on_duplicate") or DEFAULT_ON_DUPLICATE)
        )
        if on_duplicate not in ON_DUPLICATE_CHOICES:  # pragma: no cover - defensive
            on_duplicate = DEFAULT_ON_DUPLICATE

        denied = missing_create_permissions(request.user, create_missing)
        if denied:
            return _create_missing_forbidden(denied)

        job = Job.objects.create(
            tenant=request.user.tenant,
            job_type="import_commit",
            created_by=request.user,
        )
        import_job.commit_job = job
        import_job.status = ImportJob.Status.COMMITTING
        import_job.save(update_fields=["commit_job", "status", "updated_at"])

        transaction.on_commit(
            lambda: run_commit.delay(
                import_job_id=import_job.id,
                tenant_id=import_job.tenant_id,
                mapping_override=mapping_override,
                create_missing=create_missing,
                on_duplicate=on_duplicate,
                actor_id=request.user.id,
            )
        )

        return Response(ImportJobSerializer(import_job).data, status=status.HTTP_202_ACCEPTED)
