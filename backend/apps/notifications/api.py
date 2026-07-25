"""`GET/PUT/PATCH /api/v1/notifications/email-settings` (per-tenant email
delivery config: provider, sender, Brevo API key).

`NotificationPrefViewSet` above is unrelated (self-scoped, `notify.self`);
`EmailSettingsView` below is tenant-wide ADMIN configuration -- exactly one
row per tenant (`EmailSettings`'s `UniqueConstraint`), so this is a
singleton-style `RetrieveUpdateAPIView`, not a list/CRUD collection (same
shape as `apps.dashboard.api.DashboardSummaryView`/other single-resource
views, not a router-registered `ModelViewSet`).
"""

from __future__ import annotations

from rest_framework import generics, mixins, viewsets

from apps.audit.services import client_ip, write_audit_log
from apps.catalog.permissions import TenantWideReadOrManage
from apps.rbac.permission_keys import TENANT_MANAGE

from .models import EmailSettings, NotificationPref
from .permissions import NotifySelfPermission
from .serializers import EmailSettingsSerializer, NotificationPrefSerializer


class NotificationPrefViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = NotificationPrefSerializer
    permission_classes = [NotifySelfPermission]
    lookup_field = "event_type"
    lookup_value_regex = r"[^/]+"
    http_method_names = ["get", "patch", "head", "options"]

    def get_queryset(self):
        # Tenant-scoped manager (golden path step 1) + own-user-only filter
        # (step 3) -- resolved per-request, never a class-level queryset.
        return NotificationPref.objects.filter(user=self.request.user).order_by("event_type")

    def get_object(self):
        event_type = self.kwargs[self.lookup_field]
        obj, _ = NotificationPref.objects.get_or_create(
            tenant=self.request.user.tenant,
            user=self.request.user,
            event_type=event_type,
            defaults={"email_enabled": True},
        )
        self.check_object_permissions(self.request, obj)
        return obj


class EmailSettingsView(generics.RetrieveUpdateAPIView):
    """Singleton per-tenant email settings resource.

    Both read AND write require `tenant.manage` -- this is admin-only
    config (Brevo API key material), not general member-visible data, so
    `TenantWideReadOrManage`'s `view_key` is deliberately set to the SAME
    `TENANT_MANAGE` key as `manage_key` (rather than the class default
    `asset.view`), gating GET on `tenant.manage` too.
    """

    serializer_class = EmailSettingsSerializer
    permission_classes = [
        TenantWideReadOrManage(TENANT_MANAGE, view_key=TENANT_MANAGE)  # type: ignore[list-item]
    ]
    http_method_names = ["get", "put", "patch", "head", "options"]

    def get_object(self):
        # Tenant-scoped manager (golden path step 1); tenant is derived from
        # the authenticated session (`request.user.tenant`, populated by
        # `CurrentTenantMiddleware`), never client input (R4). `get_or_create`
        # makes this genuinely a singleton: the first GET/PUT for a tenant
        # that has never touched this screen transparently creates the
        # default (`console`, no key) row rather than 404ing.
        obj, _ = EmailSettings.objects.get_or_create(tenant=self.request.user.tenant)
        self.check_object_permissions(self.request, obj)
        return obj

    def _snapshot(self, instance: EmailSettings, api_key_state: str) -> dict:
        return {
            "provider": instance.provider,
            "sender_email": instance.sender_email,
            "reply_to": instance.reply_to,
            "api_key": api_key_state,
        }

    def perform_update(self, serializer):
        instance = serializer.instance
        provider_before, sender_before, reply_to_before = (
            instance.provider,
            instance.sender_email,
            instance.reply_to,
        )
        # Snapshot only whether a key was PRESENT before the write -- never
        # the key material itself -- so `before`/`after` describe an actual
        # transition (e.g. "present" -> "updated") instead of both sides
        # showing the same post-write state string.
        api_key_state_before = "present" if instance.api_key_encrypted else "absent"

        had_key_field = "api_key" in self.request.data
        if had_key_field:
            raw = self.request.data.get("api_key") or ""
            api_key_state_after = "cleared" if not raw else "updated"
        else:
            api_key_state_after = "unchanged"

        instance = serializer.save()

        before = {
            "provider": provider_before,
            "sender_email": sender_before,
            "reply_to": reply_to_before,
            "api_key": api_key_state_before,
        }
        after = self._snapshot(instance, api_key_state_after)

        # Audit every mutating write to this admin-only config (CLAUDE.md:
        # "Audit everything mutating") -- NEVER the key material itself,
        # only whether it changed (`api_key`: "unchanged"/"updated"/
        # "cleared"), matching the "before/after JSONB" shape used by every
        # other audited endpoint (`apps.catalog.api.ProjectViewSet` et al.).
        write_audit_log(
            tenant_id=instance.tenant_id,
            actor=self.request.user,
            action="email_settings.update",
            entity_type="email_settings",
            entity_id=instance.id,
            before=before,
            after=after,
            ip=client_ip(self.request),
        )
