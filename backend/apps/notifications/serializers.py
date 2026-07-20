"""Serializer for the minimal `notification-prefs` endpoint (T5.1;
`docs/api-and-ui.md`: `GET/PATCH /api/v1/notification-prefs`). Full
CRUD/richer UX is T5.6's concern (frontend "My Notifications" screen) --
this is the thin, tenant-scoped/RBAC'd read+toggle surface it needs.
"""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from .models import NotificationPref


class NotificationPrefSerializer(serializers.ModelSerializer):
    class Meta:
        model = NotificationPref
        fields = ["id", "event_type", "email_enabled", "created_at", "updated_at"]
        read_only_fields = ["id", "event_type", "created_at", "updated_at"]

    def create(self, validated_data: dict[str, Any]) -> NotificationPref:
        request = self.context["request"]
        validated_data["tenant"] = request.user.tenant
        validated_data["user"] = request.user
        return NotificationPref.objects.create(**validated_data)
