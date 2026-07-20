"""`GET /api/v1/dashboard/summary` (T5.5, `docs/api-and-ui.md`: "Aggregates
(cached)"). A single, non-CRUD, read-only endpoint -- `APIView`, not a
`ViewSet`/router registration (there is no "list of dashboards" resource to
CRUD), wired via a plain `path()` in `config/urls.py`, same reasoning as
`docs/api-and-ui.md`'s other one-off aggregate/job endpoints
(`/labels/generate`, `/jobs/{id}`).
"""

from __future__ import annotations

from rest_framework.response import Response
from rest_framework.views import APIView

from .permissions import DashboardSummaryPermission
from .serializers import DashboardSummarySerializer
from .services import get_dashboard_summary


class DashboardSummaryView(APIView):
    permission_classes = [DashboardSummaryPermission]

    def get(self, request):
        summary = get_dashboard_summary(request.user)
        serializer = DashboardSummarySerializer(summary)
        return Response(serializer.data)
