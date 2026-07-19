"""Auth endpoints (T0.6): `POST /api/v1/auth/login`, `POST /api/v1/auth/logout`,
`GET /api/v1/me`, plus `GET /api/v1/auth/csrf` (see `CsrfView` docstring).

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

from django.contrib.auth import login as django_login
from django.contrib.auth import logout as django_logout
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.common.errors import problem_response
from apps.rbac.models import Membership
from apps.tenancy.context import tenant_context
from apps.tenancy.middleware import TENANT_SESSION_KEY
from apps.tenancy.models import Tenant

from . import lockout
from .models import User
from .serializers import LoginSerializer

PROBLEM_BASE = "https://cortex.example.com/problems"


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
        "name": user.get_full_name(),
        "tenant": {
            "id": tenant.id,
            "slug": tenant.slug,
            "name": tenant.name,
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
