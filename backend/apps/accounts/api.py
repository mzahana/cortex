"""Auth endpoints (T0.6): `POST /api/v1/auth/login`, `POST /api/v1/auth/logout`,
`GET /api/v1/me`, plus `GET /api/v1/auth/csrf` (see `CsrfView` docstring).

Also `GET/POST /api/v1/users` (`UserViewSet`, near the bottom of this file) --
the "create/discover a user" gap fill. Before this, there was NO way to
create a `User` anywhere in the app except Django admin / `manage.py shell`,
and `apps.rbac.api.MembershipViewSet` could only grant a role to an EXISTING
user id. See `apps.accounts.services` module docstring for the full
gap-analysis note and the password-handling security invariants.

**R4 login-resolution path (read before touching this file):**
`apps.accounts.models.User.email` is unique only **per tenant**
(`docs/data-model.md`), so plain `django.contrib.auth.authenticate(username=
email)` / the default `ModelBackend` cannot be used here: `get_by_natural_key`
looks up by email alone and would raise `MultipleObjectsReturned` the moment
two tenants share an email (exactly the cross-tenant scenario this endpoint
must handle safely). Instead `LoginView` resolves the tenant FIRST from the
login payload's `tenant` slug, enters `apps.tenancy.context.tenant_context`
(this is what lets the RLS-subject `cortex_app` connection see that tenant's
`User` rows at all — see `apps.tenancy.db` module docstring), looks the user
up by `(tenant, email)` via the deliberately unscoped `User.all_objects`, and
verifies the Argon2 password manually with `user.check_password`. Errors are
uniform ("invalid credentials") regardless of whether the tenant, the email
within it, or the password was wrong, and a miss on tenant/user still pays the
Argon2 hashing cost (mirroring what `django.contrib.auth.backends.
ModelBackend.authenticate` itself does for a nonexistent user) so timing
doesn't reveal which case occurred.
"""

from __future__ import annotations

from django.conf import settings
from django.contrib.auth import login as django_login
from django.contrib.auth import logout as django_logout
from django.contrib.auth import update_session_auth_hash
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.http import urlencode
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.filters import BaseFilterBackend, OrderingFilter
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.audit.services import client_ip, write_audit_log
from apps.common.errors import PROBLEM_BASE, problem_response
from apps.common.pagination import BoundedPageNumberPagination
from apps.notifications.services import enqueue_transactional_email
from apps.rbac.models import Membership
from apps.rbac.permission_keys import USER_MANAGE
from apps.tenancy.context import tenant_context
from apps.tenancy.middleware import (
    SESSION_LAST_ACTIVITY_KEY,
    SESSION_LOGIN_AT_KEY,
    TENANT_SESSION_KEY,
)
from apps.tenancy.models import Tenant
from apps.tenancy.services import tenant_logo_url

from . import lockout
from .models import User
from .permissions import UserManagementPermission
from .serializers import (
    ChangePasswordSerializer,
    CreateUserSerializer,
    ForgotPasswordRequestSerializer,
    LoginSerializer,
    PasswordResetConfirmSerializer,
    UpdateMeSerializer,
    UserSerializer,
)
from .services import (
    USER_PASSWORD_CHANGE,
    USER_PASSWORD_RESET,
    USER_PASSWORD_RESET_CONFIRM,
    USER_PASSWORD_RESET_REQUEST,
    USER_PROFILE_UPDATE,
    claim_reset_token,
    create_password_reset_token,
    create_user_with_generated_password,
    peek_live_reset_token,
    reset_user_password,
    set_user_password,
    validate_new_password,
)


def _client_ip(request) -> str:
    """Best-effort client IP for the (tenant, email, ip) hard-lock key (see
    `apps.accounts.lockout`). Trusts `X-Forwarded-For` from nginx (the single,
    trusted reverse-proxy hop in this deployment — same trust boundary
    `SECURE_PROXY_SSL_HEADER` already relies on for `X-Forwarded-Proto`), and
    falls back to `REMOTE_ADDR` when absent (e.g. direct/test requests)."""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


def _invalid_credentials_response() -> Response:
    """Uniform across "no such tenant", "no such email in that tenant", and
    "wrong password" — never reveals which case occurred (R4: must not leak
    whether an email exists, in this tenant OR another one)."""
    return problem_response(
        status_code=status.HTTP_401_UNAUTHORIZED,
        title="Invalid credentials",
        detail="Invalid tenant, email, or password.",
        type_=f"{PROBLEM_BASE}/invalid-credentials",
    )


def _locked_response() -> Response:
    return problem_response(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        title="Too many failed login attempts",
        detail=(
            "This account is temporarily locked after repeated failed login "
            f"attempts. Try again in about {lockout.LOCKOUT_SECONDS // 60} minutes."
        ),
        type_=f"{PROBLEM_BASE}/login-locked",
    )


def _pay_dummy_hashing_cost(password: str) -> None:
    """Hash `password` with the real (Argon2) hasher and throw the result
    away. Mirrors `django.contrib.auth.backends.ModelBackend.authenticate`'s
    own mitigation for the "user does not exist" timing oracle (Django
    #20760), extended here to the "tenant does not exist" and "email not in
    this tenant" cases too — none of the three should be distinguishable from
    "wrong password" by response timing.
    """
    User().set_password(password)


def _serialize_me(user: User, tenant: Tenant) -> dict:
    """Shared response shape for the login endpoint and `GET /api/v1/me`
    (docs/api-and-ui.md: "Current user, memberships, effective permissions").
    Response SHAPE is unchanged from the original T0.6 cut; only the query
    pattern below changed (code-review follow-up: no N+1).

    MUST be called with `user`'s tenant already the active `tenant_context`
    — `LoginView` opens one explicitly (no session exists yet); `MeView`
    already runs inside `CurrentTenantMiddleware`'s context. `tenant` is
    passed in explicitly (never re-fetched via a lazy `user.tenant` load) so
    every call site controls exactly when that query happens.

    **No-N+1 fix:** the original cut called
    `apps.rbac.services.get_effective_permissions` once for the general pool
    and once per distinct project (`2 + 2*K` queries for K project
    memberships). This version fetches every membership + its role's granted
    permissions in a SINGLE pass (`select_related("role", "project")` +
    `prefetch_related("role__role_permissions__permission")`, which is one
    extra batched query, not one per role) and reproduces
    `get_effective_permissions`'s exact scope rule (docs/rbac.md §1) in
    Python: a tenant-wide membership's (`project_id is None`) permissions
    apply to the general pool AND every project; a project-scoped
    membership's permissions apply ONLY to that project. Total query count
    for this function is now a small constant (3: memberships + the
    prefetch's role_permissions + the prefetch's permission lookup, all
    batched), independent of how many projects/memberships the user has.
    """
    memberships_qs = (
        Membership.objects.filter(user=user)
        .select_related("role", "project")
        .prefetch_related("role__role_permissions__permission")
        .order_by("project_id", "role__key")
    )

    memberships: list[dict] = []
    tenant_wide_perms: set[str] = set()
    project_scoped_perms: dict[int, set[str]] = {}

    for m in memberships_qs:
        memberships.append(
            {
                "role": m.role.key,
                "role_name": m.role.name,
                "project_id": m.project_id,
                "project_name": m.project.name if m.project_id else None,
            }
        )
        perm_keys = {rp.permission.key for rp in m.role.role_permissions.all()}
        if m.project_id is None:
            tenant_wide_perms |= perm_keys
        else:
            project_scoped_perms.setdefault(m.project_id, set()).update(perm_keys)

    return {
        "id": user.id,
        "email": user.email,
        # `name` is the RAW stored value (may be ""), so the Account screen's
        # edit form round-trips exactly what is in the DB rather than an email
        # that only looks like a name. `display_name` is the "what to render"
        # value (`get_full_name()`: name, falling back to email) — every UI
        # surface uses that one. Splitting the two is what lets the greeting
        # show a real name once the user sets it, without the form silently
        # pre-filling the fallback.
        "name": user.name,
        "display_name": user.get_full_name(),
        "tenant": {
            "id": tenant.id,
            "slug": tenant.slug,
            "name": tenant.name,
            # Branding shown in the app chrome (`apps.tenancy.services`);
            # `None` when the lab has not uploaded a logo.
            "logo_url": tenant_logo_url(tenant),
        },
        "memberships": memberships,
        # Effective permissions on the general pool (project=None) —
        # docs/rbac.md §1: tenant-wide memberships only.
        "permissions": sorted(tenant_wide_perms),
        # Plus, per project the user holds a project-scoped membership on,
        # tenant-wide grants UNION'd in (docs/rbac.md §1 scope rule) — kept
        # separate so the UI/tests can tell general-pool power apart from
        # project-scoped power, exactly like `apps.rbac.services`.
        "project_permissions": {
            str(pid): sorted(tenant_wide_perms | proj_perms)
            for pid, proj_perms in sorted(project_scoped_perms.items())
        },
    }


@method_decorator(ensure_csrf_cookie, name="dispatch")
class CsrfView(APIView):
    """`GET /api/v1/auth/csrf` — not in `docs/api-and-ui.md`'s endpoint table;
    added so the SPA has a safe, unauthenticated way to obtain the CSRF cookie
    before the first `POST /api/v1/auth/login` (which, per project
    convention, enforces CSRF even though no session exists yet — see
    `LoginView`). Flagged as an addition for frontend-engineer/code-reviewer.
    """

    authentication_classes: list = []
    permission_classes = [AllowAny]

    def get(self, request):
        return Response({"detail": "CSRF cookie set."})


@method_decorator(csrf_protect, name="dispatch")
class LoginView(APIView):
    """`POST /api/v1/auth/login` — see module docstring for the full
    tenant-resolution path. `csrf_protect` is applied explicitly because DRF's
    `APIView.as_view()` marks the view `csrf_exempt` by default and
    `SessionAuthentication.enforce_csrf` only fires once a session user is
    already resolved — neither applies pre-login, so without this decorator
    the login endpoint would silently accept cross-site POSTs.
    """

    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        if not serializer.is_valid():
            return problem_response(
                status_code=status.HTTP_400_BAD_REQUEST,
                title="Invalid login payload",
                detail="tenant, email, and password are all required.",
                type_=f"{PROBLEM_BASE}/validation-error",
                errors=serializer.errors,
            )

        tenant_slug = serializer.validated_data["tenant"]
        email = User.all_objects.normalize_email(serializer.validated_data["email"])
        password = serializer.validated_data["password"]
        client_ip = _client_ip(request)

        if lockout.is_locked(tenant_slug, email, client_ip):
            return _locked_response()

        tenant = Tenant.objects.filter(slug=tenant_slug).first()
        if tenant is None:
            _pay_dummy_hashing_cost(password)
            lockout.register_failure(tenant_slug, email, client_ip)
            return _invalid_credentials_response()

        # Resolve the tenant BEFORE any `User` lookup: this is what lets the
        # RLS-subject connection see the tenant's rows at all (R4 — see
        # apps.tenancy.db module docstring, "Who must set it").
        with tenant_context(tenant.id):
            user = User.all_objects.filter(tenant=tenant, email=email).first()
            if user is None or not user.is_active:
                _pay_dummy_hashing_cost(password)
                lockout.register_failure(tenant_slug, email, client_ip)
                return _invalid_credentials_response()

            if not user.check_password(password):
                lockout.register_failure(tenant_slug, email, client_ip)
                return _invalid_credentials_response()

            lockout.clear_failures(tenant_slug, email, client_ip)

            # `django.contrib.auth.login()` requires `.backend` to be set
            # because we deliberately bypassed `authenticate()` (see module
            # docstring for why). `login()` also cycles the session key
            # (session-fixation protection) and is what makes
            # `CurrentTenantMiddleware` derive the right tenant on every
            # SUBSEQUENT request from `request.user.tenant_id` — this request
            # still relies on the `tenant_context` opened above.
            user.backend = "django.contrib.auth.backends.ModelBackend"  # type: ignore[attr-defined]
            django_login(request, user)
            # See `apps.tenancy.middleware.SessionTenantPreloadMiddleware`:
            # without this, RLS blocks `AuthenticationMiddleware`'s own user
            # lookup on every request AFTER this one, because it runs before
            # `CurrentTenantMiddleware` can derive the tenant from the user
            # it hasn't loaded yet. `login()` above already cycled the
            # session key (fixation protection); this key rides in the new one.
            request.session[TENANT_SESSION_KEY] = tenant.id
            # Stamp both timeout-tracking timestamps at login (read back by
            # `apps.tenancy.middleware.SessionTimeoutMiddleware` on every
            # later request). Same `now` value for both — `login_at` never
            # changes again for this session, `last_activity` is bumped by
            # the middleware as the session is used.
            now = timezone.now().timestamp()
            request.session[SESSION_LOGIN_AT_KEY] = now
            request.session[SESSION_LAST_ACTIVITY_KEY] = now

            # `tenant` was already fetched above -- pass it straight through
            # rather than letting `_serialize_me` re-derive it via a lazy
            # `user.tenant` load (code-review no-N+1 follow-up).
            payload = _serialize_me(user, tenant)

        return Response(payload, status=status.HTTP_200_OK)


class LogoutView(APIView):
    """`POST /api/v1/auth/logout`. Requires an authenticated session; DRF's
    `SessionAuthentication.enforce_csrf` already runs for this endpoint
    because a real session user resolves (unlike `LoginView`, which has none
    yet), so no extra `csrf_protect` decorator is needed here.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        django_logout(request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(APIView):
    """`GET /api/v1/me` — current user, memberships, effective permissions.
    The tenant is already in context via `CurrentTenantMiddleware`, derived
    from `request.user.tenant_id` — never from client input.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        # `request.user` comes from `AuthenticationMiddleware`'s own
        # `get_user()` query, which does NOT select_related `tenant` — refetch
        # once, explicitly, with it (code-review no-N+1 follow-up) rather than
        # letting `_serialize_me` trigger a lazy `user.tenant` load.
        user = User.all_objects.select_related("tenant").get(pk=request.user.pk)
        return Response(_serialize_me(user, user.tenant), status=status.HTTP_200_OK)

    def patch(self, request):
        """`PATCH /api/v1/me` — self-service profile edit (currently `name`
        only; see `UpdateMeSerializer` for why the field list is an explicit
        allowlist). Returns the same body as `GET /api/v1/me` so the client
        can refresh its cached session state from one round trip.

        The subject is ALWAYS `request.user` — this endpoint takes no user id,
        so it cannot be pointed at another account (in this tenant or any
        other). Audited under `user.profile_update` with a before/after `name`
        (`docs/rbac.md` §5: every mutating action is audited).
        """
        serializer = UpdateMeSerializer(data=request.data, partial=True)
        if not serializer.is_valid():
            return problem_response(
                status_code=status.HTTP_400_BAD_REQUEST,
                title="Invalid payload",
                detail="The profile update was rejected.",
                type_=f"{PROBLEM_BASE}/validation-error",
                errors=serializer.errors,
            )

        user = User.all_objects.select_related("tenant").get(pk=request.user.pk)
        before = {"name": user.name}

        if "name" in serializer.validated_data:
            user.name = serializer.validated_data["name"]
            user.save(update_fields=["name", "updated_at"])

        write_audit_log(
            tenant_id=user.tenant_id,
            actor=request.user,
            action=USER_PROFILE_UPDATE,
            entity_type="user",
            entity_id=user.id,
            before=before,
            after={"name": user.name},
            ip=client_ip(request),
        )
        return Response(_serialize_me(user, user.tenant), status=status.HTTP_200_OK)


def _weak_password_response(exc: DjangoValidationError) -> Response:
    """Map Django's `AUTH_PASSWORD_VALIDATORS` failure to an RFC-7807 400 whose
    `errors.new_password` carries the human-readable messages (the frontend
    surfaces them as field errors)."""
    return problem_response(
        status_code=status.HTTP_400_BAD_REQUEST,
        title="Password does not meet requirements",
        detail="The new password was rejected by the password policy.",
        type_=f"{PROBLEM_BASE}/validation-error",
        errors={"new_password": list(exc.messages)},
    )


class ChangePasswordView(APIView):
    """`POST /api/v1/me/password` — the signed-in user changes their OWN
    password. Requires the current password (re-auth), then validates + sets
    the new one and refreshes the session auth hash so the user stays logged
    in. Audited under `user.password_change` (actor == subject); the audit
    entry never contains password material.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "password_change"

    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data)
        if not serializer.is_valid():
            return problem_response(
                status_code=status.HTTP_400_BAD_REQUEST,
                title="Invalid payload",
                detail="current_password and new_password are both required.",
                type_=f"{PROBLEM_BASE}/validation-error",
                errors=serializer.errors,
            )

        # `request.user` already runs inside the current tenant context
        # (CurrentTenantMiddleware); refetch through the unscoped manager the
        # same way MeView does, purely to hold a concrete instance to mutate.
        user = User.all_objects.get(pk=request.user.pk)

        if not user.check_password(serializer.validated_data["current_password"]):
            return problem_response(
                status_code=status.HTTP_400_BAD_REQUEST,
                title="Current password is incorrect",
                detail="The current password you entered is incorrect.",
                type_=f"{PROBLEM_BASE}/validation-error",
                errors={"current_password": ["Incorrect password."]},
            )

        try:
            set_user_password(user, serializer.validated_data["new_password"])
        except DjangoValidationError as exc:
            return _weak_password_response(exc)

        # Keep the current session valid after the password (and thus the
        # session auth hash) changes — without this the user is logged out.
        update_session_auth_hash(request, user)

        write_audit_log(
            tenant_id=user.tenant_id,
            actor=request.user,
            action=USER_PASSWORD_CHANGE,
            entity_type="user",
            entity_id=user.id,
            before=None,
            after={"id": user.id, "email": user.email},
            ip=client_ip(request),
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


def _password_reset_generic_response() -> Response:
    """Deliberately identical whether or not the email matched a real account
    (no user-enumeration): the caller is told "if that account exists, a link
    is on its way" every time."""
    return Response(
        {
            "detail": (
                "If an account matches that tenant and email, a password-reset "
                "link has been sent."
            )
        },
        status=status.HTTP_200_OK,
    )


@method_decorator(csrf_protect, name="dispatch")
class PasswordResetRequestView(APIView):
    """`POST /api/v1/auth/password-reset/request` — unauthenticated. Body
    `{tenant, email}`. Mints a one-time token for a matching active user and
    emails the reset link via the `EmailProvider` (async). ALWAYS returns the
    same generic 200 (no enumeration).

    **Timing:** every request pays exactly one Argon2 dummy-hash cost up front,
    regardless of hit/miss. Unlike `LoginView` (whose hit path pays a real
    `check_password`), a forgot-password hit does no password verification at
    all — so without this the *miss* path (which pays the dummy hash) would be
    the SLOWER one, an inverted enumeration oracle. Paying unconditionally
    makes that dominant cost equal on both paths; the hit path's remaining
    extra work (a couple of small INSERTs, the actual send deferred to
    `on_commit`/Celery) is negligible beside Argon2. Response body/status are
    byte-identical either way, and the endpoint is throttled.
    """

    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "password_reset"

    def post(self, request):
        serializer = ForgotPasswordRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return problem_response(
                status_code=status.HTTP_400_BAD_REQUEST,
                title="Invalid payload",
                detail="tenant and email are both required.",
                type_=f"{PROBLEM_BASE}/validation-error",
                errors=serializer.errors,
            )

        tenant_slug = serializer.validated_data["tenant"]
        email = User.all_objects.normalize_email(serializer.validated_data["email"])

        # Pay the (dominant) Argon2 cost once, unconditionally — see the class
        # docstring for why this goes here rather than only on the miss paths.
        _pay_dummy_hashing_cost(email)

        tenant = Tenant.objects.filter(slug=tenant_slug).first()
        if tenant is None:
            return _password_reset_generic_response()

        with tenant_context(tenant.id):
            user = User.all_objects.filter(tenant=tenant, email=email).first()
            if user is None or not user.is_active:
                return _password_reset_generic_response()

            raw_token = create_password_reset_token(user)
            reset_url = "{base}/reset-password?{qs}".format(
                base=settings.FRONTEND_BASE_URL.rstrip("/"),
                qs=urlencode({"token": raw_token, "tenant": tenant.slug}),
            )
            # Security-critical, so NOT gated by NotificationPref (optional=False):
            # a user who can't sign in must always be able to receive this.
            enqueue_transactional_email(
                tenant_id=tenant.id,
                event_type="password_reset",
                template_id="password-reset",
                to=user.email,
                params={
                    "name": user.get_full_name(),
                    "reset_url": reset_url,
                    "tenant_name": tenant.name,
                },
                user=user,
                optional=False,
                tags=["password-reset"],
            )
            write_audit_log(
                tenant_id=tenant.id,
                actor=None,
                action=USER_PASSWORD_RESET_REQUEST,
                entity_type="user",
                entity_id=user.id,
                before=None,
                after={"id": user.id, "email": user.email},
                ip=client_ip(request),
            )

        return _password_reset_generic_response()


@method_decorator(csrf_protect, name="dispatch")
class PasswordResetConfirmView(APIView):
    """`POST /api/v1/auth/password-reset/confirm` — unauthenticated. Body
    `{tenant, token, new_password}`. Resolves the tenant from the slug (so RLS
    can see the token row), consumes a live token, validates + sets the new
    password, and audits it under `user.password_reset_confirm`.
    """

    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "password_reset"

    def post(self, request):
        serializer = PasswordResetConfirmSerializer(data=request.data)
        if not serializer.is_valid():
            return problem_response(
                status_code=status.HTTP_400_BAD_REQUEST,
                title="Invalid payload",
                detail="tenant, token, and new_password are all required.",
                type_=f"{PROBLEM_BASE}/validation-error",
                errors=serializer.errors,
            )

        tenant_slug = serializer.validated_data["tenant"]
        raw_token = serializer.validated_data["token"]
        new_password = serializer.validated_data["new_password"]

        tenant = Tenant.objects.filter(slug=tenant_slug).first()
        if tenant is None:
            return _invalid_reset_token_response()

        with tenant_context(tenant.id):
            token = peek_live_reset_token(raw_token)
            if token is None:
                return _invalid_reset_token_response()

            user = token.user
            # Validate the new password BEFORE consuming the single-use token,
            # so a rejected-weak password leaves the link usable for a retry.
            try:
                validate_new_password(user, new_password)
            except DjangoValidationError as exc:
                return _weak_password_response(exc)

            # Claim the token + set the password + audit as one unit: if the
            # password write fails after the claim, the claim rolls back too, so
            # the single-use link stays usable rather than being silently burned.
            # `claim_reset_token`'s conditional `UPDATE ... WHERE used_at IS NULL`
            # keeps concurrent confirms race-safe (only one wins the claim).
            with transaction.atomic():
                if not claim_reset_token(token):
                    return _invalid_reset_token_response()

                set_user_password(user, new_password)

                write_audit_log(
                    tenant_id=tenant.id,
                    actor=None,
                    action=USER_PASSWORD_RESET_CONFIRM,
                    entity_type="user",
                    entity_id=user.id,
                    before=None,
                    after={"id": user.id, "email": user.email},
                    ip=client_ip(request),
                )

        return Response(status=status.HTTP_204_NO_CONTENT)


def _invalid_reset_token_response() -> Response:
    return problem_response(
        status_code=status.HTTP_400_BAD_REQUEST,
        title="Invalid or expired reset link",
        detail="This password-reset link is invalid, has expired, or was already used.",
        type_=f"{PROBLEM_BASE}/invalid-reset-token",
    )


class UserSearchFilter(BaseFilterBackend):
    """`?search=` — plain case-insensitive substring match on `email`/`name`.

    Deliberately much simpler than `apps.assets.api.AssetSearchFilter` (no
    FTS/trigram indexes needed for what is expected to be a small,
    per-tenant user list).
    """

    search_param = "search"

    def filter_queryset(self, request, queryset, view):
        search = request.query_params.get(self.search_param, "").strip()
        if not search:
            return queryset
        return queryset.filter(Q(email__icontains=search) | Q(name__icontains=search))


def _user_snapshot(user: User) -> dict:
    """Audit before/after snapshot for `user.manage` user-creation —
    deliberately id/email/name ONLY. Must NEVER include `password`/
    `password_hash`/anything derived from the plaintext generated in
    `apps.accounts.services.create_user_with_generated_password` (task
    requirement / CLAUDE.md "no secrets in the audit log").
    """
    return {"id": user.id, "email": user.email, "name": user.name}


class UserViewSet(mixins.ListModelMixin, mixins.CreateModelMixin, viewsets.GenericViewSet):
    """`GET/POST /api/v1/users` — see module docstring / `apps.accounts.
    services` for why this exists.

    Tenant scoping (golden-path step 2): `get_queryset()` builds
    `User.objects...` (tenant-scoped, fail-closed manager) fresh per request.

    RBAC (golden-path step 3): `UserManagementPermission` — `create` and
    `reset_password` are Admin-only (tenant-wide `user.manage`); `list` is any
    scope.

    No `retrieve`/`update`/`destroy` route is exposed here — this endpoint's
    only job is (a) mint a new account, (b) let an existing `user.manage`
    holder discover a user id to hand to `POST /api/v1/memberships`, and
    (c) `reset_password`: regenerate another user's password (Admin-only).
    """

    permission_classes = [UserManagementPermission]
    http_method_names = ["get", "post", "head", "options"]
    filter_backends = [UserSearchFilter, OrderingFilter]
    ordering_fields = ["email", "name", "created_at"]
    pagination_class = BoundedPageNumberPagination

    def get_serializer_class(self):
        if self.action == "create":
            return CreateUserSerializer
        return UserSerializer

    def get_queryset(self):
        # Tenant-scoped manager, resolved per-request (never a class-level
        # `queryset = ...` — same reasoning as `apps.rbac.api.
        # MembershipViewSet.get_queryset`).
        return User.objects.all().order_by("email")

    def create(self, request, *args, **kwargs):
        serializer = CreateUserSerializer(data=request.data)
        if not serializer.is_valid():
            return problem_response(
                status_code=status.HTTP_400_BAD_REQUEST,
                title="Invalid user payload",
                detail="Could not create user.",
                type_=f"{PROBLEM_BASE}/validation-error",
                errors=serializer.errors,
            )

        created = create_user_with_generated_password(
            email=serializer.validated_data["email"],
            name=serializer.validated_data.get("name", ""),
        )

        # Audited under `user.manage` (docs/rbac.md §5), same as Membership
        # create/destroy — before/after snapshot NEVER contains the
        # password: `_user_snapshot()` only ever returns id/email/name (see
        # its docstring), so there is no code path here by which the
        # generated password could reach `write_audit_log`/the AuditLog
        # table.
        write_audit_log(
            tenant_id=created.user.tenant_id,
            actor=request.user,
            action=USER_MANAGE,
            entity_type="user",
            entity_id=created.user.id,
            before=None,
            after=_user_snapshot(created.user),
            ip=client_ip(request),
        )

        # The ONLY time the generated password is ever returned — added to
        # the plain `UserSerializer` shape rather than being a field on it,
        # so `GET /api/v1/users` (which reuses the same serializer) can
        # never accidentally include it.
        payload = UserSerializer(created.user).data
        payload["password"] = created.initial_password
        return Response(payload, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="reset-password")
    def reset_password(self, request, pk=None):
        """`POST /api/v1/users/{id}/reset-password` — Admin regenerates another
        user's password. `get_object()` resolves the id through the tenant-
        scoped queryset, so a cross-tenant id 404s (R4) before any reset. The
        fresh one-time password is returned in the body exactly ONCE (same
        contract/handling as create), and never reaches the audit log
        (`_user_snapshot` is id/email/name only).
        """
        user = self.get_object()
        new_password = reset_user_password(user)

        write_audit_log(
            tenant_id=user.tenant_id,
            actor=request.user,
            action=USER_PASSWORD_RESET,
            entity_type="user",
            entity_id=user.id,
            before=None,
            after=_user_snapshot(user),
            ip=client_ip(request),
        )

        payload = UserSerializer(user).data
        payload["password"] = new_password
        return Response(payload, status=status.HTTP_200_OK)
