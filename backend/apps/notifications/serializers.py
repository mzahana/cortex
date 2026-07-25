"""Serializer for the minimal `notification-prefs` endpoint (T5.1;
`docs/api-and-ui.md`: `GET/PATCH /api/v1/notification-prefs`). Full
CRUD/richer UX is T5.6's concern (frontend "My Notifications" screen) --
this is the thin, tenant-scoped/RBAC'd read+toggle surface it needs.
"""

from __future__ import annotations

from rest_framework import serializers

from .crypto import encrypt_api_key
from .models import EmailSettings, NotificationPref


class NotificationPrefSerializer(serializers.ModelSerializer):
    class Meta:
        model = NotificationPref
        fields = ["id", "event_type", "email_enabled", "created_at", "updated_at"]
        read_only_fields = ["id", "event_type", "created_at", "updated_at"]


class EmailSettingsSerializer(serializers.ModelSerializer):
    """`GET/PUT/PATCH /api/v1/notifications/email-settings` -- tenant admins
    configure email delivery (provider, sender, Brevo API key) from the UI.

    `api_key` is write-only input, never a model field directly: on
    write, `update()` encrypts it into `api_key_encrypted`/derives
    `api_key_last4` (`apps.notifications.crypto`). The raw/encrypted key
    material is NEVER included in `to_representation` output -- only
    `has_api_key`/`api_key_last4` (a harmless last-4-chars hint, same UX
    pattern as most "add a payment card" UIs).

    Field omitted entirely from the request body -> key left untouched.
    `api_key=""` (explicitly blank) -> clears the stored key.
    """

    has_api_key = serializers.SerializerMethodField()
    api_key = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = EmailSettings
        fields = [
            "provider",
            "sender_email",
            "reply_to",
            "api_key",
            "api_key_last4",
            "has_api_key",
            "updated_at",
        ]
        read_only_fields = ["api_key_last4", "updated_at"]

    def get_has_api_key(self, obj: EmailSettings) -> bool:
        return bool(obj.api_key_encrypted)

    def _apply_api_key(self, instance: EmailSettings, raw_api_key: str) -> None:
        instance.api_key_encrypted = encrypt_api_key(raw_api_key)
        instance.api_key_last4 = raw_api_key[-4:] if raw_api_key else ""

    def update(self, instance: EmailSettings, validated_data: dict) -> EmailSettings:
        # `.pop(..., None)` alone can't distinguish "omitted" from
        # "explicitly None" for a CharField (DRF never produces `None` for a
        # non-`allow_null` CharField), so presence-in-`validated_data` is the
        # correct "was this field in the request?" check -- matches the
        # "omitted => untouched, blank => cleared" contract in the class
        # docstring.
        has_api_key_field = "api_key" in validated_data
        raw_api_key = validated_data.pop("api_key", None)
        instance = super().update(instance, validated_data)
        if has_api_key_field:
            self._apply_api_key(instance, raw_api_key or "")
            instance.save(update_fields=["api_key_encrypted", "api_key_last4"])
        return instance

    def create(self, validated_data: dict) -> EmailSettings:  # pragma: no cover - singleton
        # `EmailSettings` is get_or_create'd by the view (singleton per
        # tenant) -- `create()` is defined for completeness/serializer
        # symmetry only, not expected to be exercised via this API surface.
        raw_api_key = validated_data.pop("api_key", None)
        instance = super().create(validated_data)
        if raw_api_key is not None:
            self._apply_api_key(instance, raw_api_key)
            instance.save(update_fields=["api_key_encrypted", "api_key_last4"])
        return instance
