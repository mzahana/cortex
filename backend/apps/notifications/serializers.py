"""Serializer for the minimal `notification-prefs` endpoint (T5.1;
`docs/api-and-ui.md`: `GET/PATCH /api/v1/notification-prefs`). Full
CRUD/richer UX is T5.6's concern (frontend "My Notifications" screen) --
this is the thin, tenant-scoped/RBAC'd read+toggle surface it needs.
"""

from __future__ import annotations

from rest_framework import serializers

from .models import NotificationPref


class NotificationPrefSerializer(serializers.ModelSerializer):
    class Meta:
        model = NotificationPref
        fields = ["id", "event_type", "email_enabled", "created_at", "updated_at"]
        read_only_fields = ["id", "event_type", "created_at", "updated_at"]
