"""Response shape for `GET /api/v1/dashboard/summary` (T5.5).

Plain read-only (`source="*"`-style passthrough) serializers over the dict
`apps.dashboard.services.get_dashboard_summary` returns -- there is no model
behind this endpoint (pure aggregation), so these exist only to document and
validate the response shape, matching DRF conventions used elsewhere in this
codebase for non-model responses.
"""

from __future__ import annotations

from rest_framework import serializers


class CategoryTotalSerializer(serializers.Serializer):
    category_id = serializers.IntegerField(allow_null=True)
    category_name = serializers.CharField(allow_null=True)
    count = serializers.IntegerField()


class ProjectAllocationSerializer(serializers.Serializer):
    project_id = serializers.IntegerField(allow_null=True)
    project_name = serializers.CharField()
    count = serializers.IntegerField()


class DashboardSummarySerializer(serializers.Serializer):
    totals_by_category = CategoryTotalSerializer(many=True)
    currently_out = serializers.IntegerField()
    overdue = serializers.IntegerField()
    low_stock = serializers.IntegerField()
    upcoming_reservations = serializers.IntegerField()
    upcoming_reservations_window_days = serializers.IntegerField()
    per_project_allocation = ProjectAllocationSerializer(many=True)
    generated_at = serializers.DateTimeField()
