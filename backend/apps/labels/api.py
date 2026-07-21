"""`POST /api/v1/labels/generate` (T4.5): enqueues a Celery job that renders
an Avery-sheet label PDF; the response is the `Job` (poll `GET /api/v1/jobs/
{id}`, `apps.jobs.api.JobRetrieveView`, until `status == "succeeded"`, then
follow `download_url`).

Tenant scoping (golden-path step 2): `Asset.objects.filter(id__in=...)` is
the tenant-scoped manager — an id from another tenant simply never appears
in the result set, same "matches nothing" reasoning
`apps.assets.api.AssetFilterSet`'s module docstring already documents for
cross-tenant FK filters. Combined with the RBAC scope filter below, the
final `ordered_assets`/`asset_ids` a job is ever created for can only ever
be assets the caller's own tenant AND RBAC scope actually cover — never
"rejected with an error", just silently excluded (fail-closed, same
existence-leak-avoidance posture as `AssetResolveView`), UNLESS that leaves
zero assets, which 400s (nothing to generate).

RBAC (golden-path step 3): `LabelGeneratePermission` gates entry
("holds `label.generate` somewhere"); THIS view is what makes the real,
per-asset, scope-correct decision — `user_has_permission(request.user,
LABEL_GENERATE, project=asset.project_id)` for every requested asset,
exactly `apps.assets.permissions.AssetPermission`'s object-level rule
(docs/rbac.md §1 union-of-memberships).

Async dispatch (golden-path step 4, CLAUDE.md "slow work runs in Celery"):
the `Job` row is created (status=queued) INSIDE the request's transaction,
and the task is enqueued via `transaction.on_commit(...)` so it can never
start running against a `Job` row the transaction hasn't committed yet —
identical convention to `apps.notifications.services.
enqueue_transactional_email` (see that module for the pattern this mirrors).
"""

from __future__ import annotations

from django.db import transaction
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.assets.models import Asset
from apps.common.errors import problem_response
from apps.jobs.models import Job
from apps.jobs.serializers import JobSerializer
from apps.rbac.permission_keys import LABEL_GENERATE
from apps.rbac.services import user_has_permission

from .permissions import LabelGeneratePermission
from .serializers import LabelGenerateRequestSerializer
from .tasks import generate_label_pdf


class LabelGenerateView(APIView):
    permission_classes = [permissions.IsAuthenticated, LabelGeneratePermission]

    def post(self, request):
        serializer = LabelGenerateRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        requested_ids: list[int] = serializer.validated_data["asset_ids"]
        template_key: str = serializer.validated_data["template"]

        # Tenant-scoped fetch (never `all_objects`) -- a nonexistent OR
        # cross-tenant id simply matches no row here.
        tenant_assets = {asset.id: asset for asset in Asset.objects.filter(id__in=requested_ids)}

        # RBAC scope filter (the real, per-asset, scope-correct check --
        # see module docstring). Preserves the caller's requested order.
        allowed_ids = [
            asset_id
            for asset_id in requested_ids
            if asset_id in tenant_assets
            and user_has_permission(
                request.user, LABEL_GENERATE, project=tenant_assets[asset_id].project_id
            )
        ]

        if not allowed_ids:
            return problem_response(
                status_code=status.HTTP_400_BAD_REQUEST,
                title="No assets available",
                detail=(
                    "None of the requested asset ids exist in your tenant and are "
                    "within your label.generate scope."
                ),
            )

        job = Job.objects.create(
            tenant=request.user.tenant,
            job_type="label_pdf",
            params={"asset_ids": allowed_ids, "template": template_key},
            created_by=request.user,
        )

        transaction.on_commit(
            lambda: generate_label_pdf.delay(
                job_id=str(job.id),
                tenant_id=job.tenant_id,
                asset_ids=allowed_ids,
                template_key=template_key,
            )
        )

        return Response(JobSerializer(job).data, status=status.HTTP_202_ACCEPTED)
