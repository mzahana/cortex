"""Request body validation for `POST /api/v1/labels/generate` (T4.5)."""

from __future__ import annotations

from rest_framework import serializers

from .templates import SHEET_TEMPLATES


class LabelGenerateRequestSerializer(serializers.Serializer):
    asset_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=False,
        max_length=500,  # sane upper bound (a "sheet run", not a full-tenant export)
    )
    template = serializers.ChoiceField(choices=list(SHEET_TEMPLATES.keys()))

    def validate_asset_ids(self, value: list[int]) -> list[int]:
        # De-dupe while preserving the caller's first-seen order (a repeated
        # id would otherwise print the same label twice, which is never the
        # intent of a "select N assets" UI).
        seen: set[int] = set()
        deduped: list[int] = []
        for asset_id in value:
            if asset_id not in seen:
                seen.add(asset_id)
                deduped.append(asset_id)
        return deduped
