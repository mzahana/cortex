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
from rest_framework import generics, status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.services import client_ip, write_audit_log
from apps.catalog.permissions import TenantWideReadOrManage
from apps.common.errors import problem_response
from apps.rbac.permission_keys import TENANT_MANAGE

from .models import SessionSettings
from .serializers import SessionSettingsSerializer, TenantBrandingSerializer
from .services import clear_tenant_logo, save_tenant_logo


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


class TenantLogoView(APIView):
    """`GET/POST/DELETE /api/v1/tenancy/logo` — the tenant's (lab's) branding
    logo, shown in the app chrome after login.

    - `GET` is open to any authenticated member: the logo is already part of
      every `GET /api/v1/me` response (it is chrome every member sees), so
      gating the read would protect nothing.
    - `POST` (multipart `file`) and `DELETE` require `tenant.manage` —
      branding is tenant configuration, same admin-only bar as
      `SessionSettingsView`/`EmailSettingsView`.

    Tenant isolation (R4): the target tenant is ALWAYS `request.user.tenant`,
    derived from the session by `CurrentTenantMiddleware`. This endpoint takes
    no tenant id/slug from the client at all, so there is no shape in which it
    can write another tenant's row.

    Both mutations are audited (`tenant.logo.update` / `tenant.logo.delete`)
    with a before/after carrying the storage key + filename only — never the
    bytes.
    """

    parser_classes = [MultiPartParser, FormParser]
    permission_classes = [TenantWideReadOrManage(TENANT_MANAGE)]  # type: ignore[list-item]

    def _tenant(self):
        return self.request.user.tenant  # type: ignore[union-attr]

    def _response(self, tenant) -> Response:
        return Response(TenantBrandingSerializer(tenant).data, status=status.HTTP_200_OK)

    def get(self, request):
        return self._response(self._tenant())

    def post(self, request):
        tenant = self._tenant()
        uploaded_file = request.FILES.get("file")
        if uploaded_file is None:
            return problem_response(
                status_code=status.HTTP_400_BAD_REQUEST,
                title="Missing file",
                detail="A multipart 'file' field is required.",
            )

        before = {
            "logo_storage_key": tenant.logo_storage_key,
            "logo_filename": tenant.logo_filename,
        }
        # Validation (size + content-type/extension allowlist) happens inside
        # `save_tenant_logo` BEFORE any bytes are written; a rejection raises
        # `serializers.ValidationError`, which the RFC-7807 exception handler
        # turns into a clean 400 exactly like every other validation error.
        tenant = save_tenant_logo(tenant=tenant, uploaded_file=uploaded_file)

        write_audit_log(
            tenant_id=tenant.id,
            actor=request.user,
            action="tenant.logo.update",
            entity_type="tenant",
            entity_id=tenant.id,
            before=before,
            after={
                "logo_storage_key": tenant.logo_storage_key,
                "logo_filename": tenant.logo_filename,
            },
            ip=client_ip(request),
        )
        return self._response(tenant)

    def delete(self, request):
        tenant = self._tenant()
        if not tenant.logo_storage_key:
            # Idempotent: nothing to remove, nothing to audit.
            return self._response(tenant)

        before = {
            "logo_storage_key": tenant.logo_storage_key,
            "logo_filename": tenant.logo_filename,
        }
        tenant = clear_tenant_logo(tenant=tenant)

        write_audit_log(
            tenant_id=tenant.id,
            actor=request.user,
            action="tenant.logo.delete",
            entity_type="tenant",
            entity_id=tenant.id,
            before=before,
            after={"logo_storage_key": "", "logo_filename": ""},
            ip=client_ip(request),
        )
        return self._response(tenant)
