"""Serializer for `GET/PATCH /api/v1/tenancy/session-settings`
(per-tenant idle/absolute session timeout policy).

Mirrors `apps.notifications.serializers.EmailSettingsSerializer` — the one
existing precedent for "env-only config moved to a per-tenant DB-backed,
admin-editable setting".
"""

from __future__ import annotations

from rest_framework import serializers

from .models import SessionSettings


class SessionSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = SessionSettings
        fields = ["idle_timeout_minutes", "absolute_timeout_hours", "updated_at"]
        read_only_fields = ["updated_at"]
        # Bounds (5-480 / 1-168) come free from the model field's own
        # `MinValueValidator`/`MaxValueValidator` — `ModelSerializer` copies
        # them onto the generated fields automatically, no extra validation
        # code needed here.
