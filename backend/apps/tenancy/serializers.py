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


class TenantBrandingSerializer(serializers.Serializer):
    """Read shape for `GET/POST/DELETE /api/v1/tenancy/logo` — the lab's own
    identity as the UI shows it (name + logo URL). Deliberately NOT a
    `ModelSerializer` over `Tenant`: nothing here is client-writable (the logo
    is set by uploading a file, the name is not editable through this
    endpoint), so a read-only projection is the honest shape.

    `logo_url` is `MEDIA_URL + storage_key` (nginx-served volume), or `null`
    when the tenant has no logo — the UI falls back to its initials.
    """

    id = serializers.IntegerField(read_only=True)
    slug = serializers.SlugField(read_only=True)
    name = serializers.CharField(read_only=True)
    logo_url = serializers.SerializerMethodField()
    logo_filename = serializers.CharField(read_only=True)
    logo_updated_at = serializers.DateTimeField(read_only=True)

    def get_logo_url(self, obj) -> str | None:
        from .services import tenant_logo_url

        return tenant_logo_url(obj)
