"""`GET /api/v1/jobs/{id}` (T4.5) — poll a `Job`'s status; the response's
`download_url` is populated once `status == "succeeded"`.

Tenant scoping (golden-path step 2): `get_queryset()` builds `Job.objects...`
(tenant-scoped, fail-closed manager) fresh per request.

RBAC (golden-path step 3, task instructions — deliberately narrow): a job is
a private polling handle for whoever created it, not a shared team resource
(no scope-sharing model, unlike Asset). `get_queryset()` additionally filters
to `created_by=request.user` — someone else's job (even same tenant, even an
Admin) 404s exactly like a nonexistent/cross-tenant id would, matching the
existing "never distinguish doesn't-exist from can't-see" posture used
throughout this codebase (e.g. `apps.assets.api.AssetResolveView`).
"""

from __future__ import annotations

from rest_framework import generics, permissions

from .models import Job
from .serializers import JobSerializer


class JobRetrieveView(generics.RetrieveAPIView):
    serializer_class = JobSerializer
    permission_classes = [permissions.IsAuthenticated]
    lookup_field = "id"
    lookup_url_kwarg = "job_id"

    def get_queryset(self):
        return Job.objects.filter(created_by=self.request.user)
