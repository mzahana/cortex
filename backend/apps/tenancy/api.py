"""`GET/PATCH /api/v1/tenancy/session-settings` — per-tenant idle/absolute
session timeout policy, admin-editable from the UI instead of only via
`SESSION_COOKIE_AGE`.

Singleton-style `RetrieveUpdateAPIView`, exactly one row per tenant
(`SessionSettings`'s `UniqueConstraint`), same shape as
`apps.notifications.api.EmailSettingsView` — deliberately mirrored, this is
the second consumer of that "env-only config -> per-tenant DB-backed
setting" pattern.
"""

from __future__ import annotations

from django.core.cache import cache
from django.db import transaction
from rest_framework import generics

from apps.audit.services import client_ip, write_audit_log
from apps.catalog.permissions import TenantWideReadOrManage
from apps.rbac.permission_keys import TENANT_MANAGE

from .models import SessionSettings
from .serializers import SessionSettingsSerializer


class SessionSettingsView(generics.RetrieveUpdateAPIView):
    """Singleton per-tenant session-timeout settings resource.

    Both read AND write require `tenant.manage` -- this is admin-only
    security policy (how long a stolen/idle session stays valid for every
    member of the tenant), not general member-visible data, so
    `TenantWideReadOrManage`'s `view_key` is deliberately set to the SAME
    `TENANT_MANAGE` key as `manage_key` (rather than the class default
    `asset.view`), gating GET on `tenant.manage` too -- identical reasoning
    to `apps.notifications.api.EmailSettingsView`.
    """

    serializer_class = SessionSettingsSerializer
    permission_classes = [
        TenantWideReadOrManage(TENANT_MANAGE, view_key=TENANT_MANAGE)  # type: ignore[list-item]
    ]
    http_method_names = ["get", "put", "patch", "head", "options"]

    def get_object(self):
        # Tenant-scoped manager (golden path step 1); tenant is derived from
        # the authenticated session (`request.user.tenant`, populated by
        # `CurrentTenantMiddleware`), never client input (R4). `get_or_create`
        # makes this genuinely a singleton: the first GET/PATCH for a tenant
        # that has never touched this screen transparently creates the
        # default (60min idle / 24h absolute) row rather than 404ing.
        obj, _ = SessionSettings.objects.get_or_create(
            tenant=self.request.user.tenant  # type: ignore[union-attr]
        )
        self.check_object_permissions(self.request, obj)
        return obj

    def perform_update(self, serializer):
        instance = serializer.instance
        before = {
            "idle_timeout_minutes": instance.idle_timeout_minutes,
            "absolute_timeout_hours": instance.absolute_timeout_hours,
        }

        instance = serializer.save()

        after = {
            "idle_timeout_minutes": instance.idle_timeout_minutes,
            "absolute_timeout_hours": instance.absolute_timeout_hours,
        }

        # Invalidate the 60s cache `apps.tenancy.middleware.
        # get_session_timeout_settings` populates, so the very next request
        # after this save is checked against the NEW bounds rather than a
        # stale cached value for up to 60s.
        #
        # Deferred to `transaction.on_commit`: this view runs under
        # `ATOMIC_REQUESTS=True`, so without this, the cache delete would
        # execute BEFORE this request's transaction actually commits. A
        # concurrent request landing in that window could re-read the
        # still-pre-commit (old) DB row via `get_session_timeout_settings`'s
        # cache-miss path and re-populate the cache with the STALE value for
        # another 60s, even after this PATCH's new value has committed --
        # defeating "near-immediate invalidation on config change." Deferring
        # to `on_commit` guarantees the delete only runs once the new value is
        # actually durably visible to every other connection.
        tenant_id = instance.tenant_id
        transaction.on_commit(lambda: cache.delete(f"session_settings:{tenant_id}"))

        # Audit every mutating write to this admin-only security policy
        # (CLAUDE.md: "Audit everything mutating") -- same before/after JSONB
        # shape as every other audited endpoint.
        write_audit_log(
            tenant_id=instance.tenant_id,
            actor=self.request.user,
            action="session_settings.update",
            entity_type="session_settings",
            entity_id=instance.id,
            before=before,
            after=after,
            ip=client_ip(self.request),
        )
