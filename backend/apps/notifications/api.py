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

from rest_framework import generics, mixins, status, viewsets
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.audit.services import client_ip, write_audit_log
from apps.catalog.permissions import TenantWideReadOrManage
from apps.common.errors import problem_response
from apps.rbac.permission_keys import TENANT_MANAGE

from .models import EmailLog, EmailSettings, NotificationPref
from .permissions import NotifySelfPermission
from .providers import get_email_provider
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


class EmailSettingsTestView(APIView):
    """`POST /api/v1/notifications/email-settings/test` -- lets a tenant
    admin who just configured Brevo (provider, sender, API key) immediately
    verify the saved config actually works, instead of waiting for a real
    domain event to trigger a send.

    Uses `EmailProvider.send_test_email(...)`, NOT `send_transactional`
    (deliberately): the 6 real business emails and the test email both
    build their subject/HTML locally now (`apps.notifications.content` /
    `providers.py`) and need no Brevo dashboard template at all -- but
    `send_test_email` still sends its own fixed, hardcoded subject/body
    directly rather than going through a "real" notification type, so
    testing only ever needs API key + sender, the same two things the
    button is gated on client-side.

    Same `tenant.manage` gate as `EmailSettingsView` -- this is the same
    admin-only surface, just an action rather than a read/write on the
    resource itself.

    **Recipient is always `request.user.email`, never client input.** This
    endpoint sends using the TENANT's stored Brevo credentials; if it
    accepted an arbitrary `to` address, any tenant admin could turn it into
    a spam/phishing relay. The whole point is "verify MY configuration
    works", not "send an email to anyone" -- so the request body's `to` (if
    present) is simply never read. Note this narrows, but doesn't fully
    close, the abuse surface: a `tenant.manage` holder can still reach an
    address of their choosing indirectly by provisioning a new user via
    `user.manage` (itself audited: `user.manage`/`role.assign`) and sending
    to that account's email -- an intentional, audited, multi-step path,
    not a gap in this endpoint. It also means a tenant with no Brevo
    account of their own (sending through the operator's global
    `BREVO_API_KEY`) can, slowly and audibly, spend the operator's Brevo
    reputation/quota via this endpoint -- one more reason for the
    `email_test` throttle scope below, alongside the availability
    rationale.

    **Sends synchronously, inline in the request -- a deliberate, narrow
    exception to CLAUDE.md's "slow work runs in Celery" rule.** Every other
    send in this app (`apps.notifications.services.enqueue_transactional_
    email`) is fire-and-forget: a domain event (checkout going overdue, low
    stock, a reservation) that can fan out to many recipients and where
    nobody is watching the request in real time, so async + retry is
    strictly better. This is the opposite shape: one bounded, admin-
    triggered diagnostic click, exactly one recipient (the caller), and the
    entire point is synchronous pass/fail feedback on THIS request --
    "did my key/template work" -- not a background log to check later.
    `BrevoProvider.send_transactional` bounds this with its own 10s `urllib`
    timeout (`providers.py`) -- note that's a per-socket-operation timeout,
    not a hard wall-clock cap, so a slow-drip remote server can still hold a
    worker somewhat longer; `ConsoleProvider` (the default) returns
    instantly. Because this is a synchronous outbound HTTP call on a
    request thread rather than Celery, an unthrottled version of this
    endpoint would let a handful of concurrent test-sends occupy every sync
    Gunicorn worker (`docker-compose*.yml`, `GUNICORN_WORKERS`, default 2)
    for the duration of each call, queuing every OTHER tenant's requests
    behind it -- a cross-tenant availability risk `tenant.manage`-gating
    does NOT contain, since the worker pool is shared platform-wide. Given a
    dedicated, tight `ScopedRateThrottle` below (`email_test`, same pattern
    as `login`/`password_reset` in `config/settings/base.py`), matching the
    "sensitive, expensive, admin-adjacent action" precedent those already
    set.

    No `write_audit_log` entry: this doesn't mutate `EmailSettings` (the
    audited resource) and, per the precedent of this codebase's other
    non-CRUD "trigger an action" endpoints (`apps.labels.api.
    LabelGenerateView`, `apps.imports` job triggers), a single bounded
    trigger action is not itself audit-logged -- the `EmailLog` row written
    below (`event_type="test_email"`) already gives a permanent, tenant-
    scoped record of who triggered it, when, and whether it succeeded, the
    same as every other send this app makes.
    """

    permission_classes = [
        TenantWideReadOrManage(TENANT_MANAGE, view_key=TENANT_MANAGE)  # type: ignore[list-item]
    ]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "email_test"

    def post(self, request):
        user = request.user
        tenant = user.tenant
        recipient = user.email

        # `get_email_provider()` moved INSIDE the try (it used to run
        # before it): for a tenant with a stored key, resolving the
        # provider decrypts it (`BrevoProvider.__init__` ->
        # `crypto.decrypt_api_key`), which raises `cryptography.fernet.
        # InvalidToken` -- not `EmailSendError` -- if
        # `EMAIL_SETTINGS_ENCRYPTION_KEY` was ever rotated/regenerated
        # after the key was stored. That's exactly the moment an admin is
        # likely to click this button, so it must fail the same clean,
        # audited way as any other send failure rather than bubbling up
        # as an unhandled 500 with no `EmailLog` row at all.
        try:
            provider = get_email_provider()
            provider_name = type(provider).__name__
            result = provider.send_test_email(
                to=recipient,
                tenant_name=tenant.name,
                triggered_by=user.get_full_name() or user.email,
            )
        except Exception as exc:
            # Catches `EmailSendError` (the expected fail-closed case: no
            # API key, no sender configured, a real Brevo HTTP
            # failure) AND anything unexpected (e.g. a decrypt failure
            # above) -- either way this is a diagnostic endpoint whose
            # entire job is to report pass/fail without ever 500ing, and
            # to always leave a durable `EmailLog` row behind (this app's
            # audit trail for sends, see the class docstring).
            provider_name = provider_name if "provider_name" in locals() else "unknown"
            # `str(exc)` can be BLANK for some exception types (e.g.
            # `cryptography.fernet.InvalidToken`, raised on a rotated
            # `EMAIL_SETTINGS_ENCRYPTION_KEY`, stringifies to `""`) -- fall
            # back to `"<ClassName>: <message>"` so the admin-facing 400
            # `detail` and the `EmailLog.error` audit field are never blank,
            # which would look like a broken/successful-looking failure.
            detail = str(exc) or f"{type(exc).__name__} (no further detail available)"
            EmailLog.all_objects.create(
                tenant=tenant,
                user=user,
                recipient=recipient,
                event_type="test_email",
                provider=provider_name,
                status=EmailLog.Status.FAILED,
                error=detail,
            )
            # Built directly via `problem_response` (NOT `raise
            # ValidationError(...)`) -- deliberately. DRF's own
            # `exception_handler` calls `set_rollback()` for every
            # `APIException` (`rest_framework/views.py`), which marks the
            # WHOLE request's `ATOMIC_REQUESTS` transaction for rollback --
            # that would silently discard the `failed` `EmailLog` row just
            # written above along with everything else in the request.
            # Returning a normal `Response` (same RFC-7807 problem+json
            # shape `rfc7807_exception_handler` produces for a real
            # `ValidationError`, via the same helper the T0.6 login view
            # uses for its own hand-written error responses) lets the
            # request's transaction commit normally, so the audit trail of
            # a failed test-send actually survives.
            return problem_response(
                status_code=status.HTTP_400_BAD_REQUEST,
                title="ValidationError",
                detail=detail,
            )

        EmailLog.all_objects.create(
            tenant=tenant,
            user=user,
            recipient=recipient,
            event_type="test_email",
            provider=provider_name,
            status=EmailLog.Status.SENT,
            provider_message_id=result.provider_message_id,
        )

        return Response(
            {"status": "sent", "provider": provider_name, "sent_to": recipient},
            status=status.HTTP_200_OK,
        )
