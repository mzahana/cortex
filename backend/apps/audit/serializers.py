"""Read-only serializer for `GET /api/v1/audit` (T5.3)."""

from __future__ import annotations

from rest_framework import serializers

from .models import AuditLog


class AuditLogSerializer(serializers.ModelSerializer):
    actor_email = serializers.CharField(source="actor.email", read_only=True, default=None)
    actor_name = serializers.CharField(source="actor.name", read_only=True, default=None)

    class Meta:
        model = AuditLog
        fields = [
            "id",
            "actor",
            "actor_email",
            "actor_name",
            "action",
            "entity_type",
            "entity_id",
            "before",
            "after",
            "ip",
            "created_at",
        ]
        read_only_fields = fields
