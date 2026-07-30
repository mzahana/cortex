"""`CurrentTenantMiddleware` — the ONLY writer of the current-tenant context
for real HTTP requests (T0.4).

Placement matters: this must run **after**
`django.contrib.sessions.middleware.SessionMiddleware` and
`django.contrib.auth.middleware.AuthenticationMiddleware` in `MIDDLEWARE`
(see `config/settings/base.py`), because it reads `request.user` — a
`SimpleLazyObject` that resolves the authenticated user strictly from the
signed, server-side session (Redis-backed; see docs/architecture.md §4).

**R4 guarantee:** the tenant is derived from `request.user.tenant_id`. There
is no code path here that reads a header, query param, cookie value, or
request body field to pick the tenant — the client never gets a vote. An
anonymous/unauthenticated request simply leaves the tenant context unset,
which makes any tenant-scoped ORM access during that request raise
`TenantContextError` (fail-closed) rather than silently return unfiltered
data.

**T0.5 coordination point:** entering `tenant_context()` pushes the tenant into
BOTH the app-level contextvar and the Postgres session GUC `app.current_tenant`
that RLS reads (see `apps.tenancy.context` / `apps.tenancy.db`), and clears it
again on exit. That single entry point is what makes RLS the real
defense-in-depth backstop on every path — HTTP here, plus Celery/commands that
use `tenant_context()` directly — rather than an inert set of policies that
never receive the tenant. For an anonymous request the tenant is `None`, so the
GUC is set to '' -> RLS returns zero rows (fail-closed), matching the contextvar
being unset. No value leaks across requests: `tenant_context` clears the GUC in
its own `finally`.
"""

from __future__ import annotations

import time

from django.core.cache import cache
from django.http import JsonResponse

from apps.common.errors import PROBLEM_BASE, PROBLEM_CONTENT_TYPE

from .context import tenant_context

# Session key written by `apps.accounts.api.LoginView` right after a
# successful login, read back by `SessionTenantPreloadMiddleware` below.
TENANT_SESSION_KEY = "tenant_id"

# Session keys written by `apps.accounts.api.LoginView` alongside
# `TENANT_SESSION_KEY`, read back by `SessionTimeoutMiddleware` below (epoch
# floats, `timezone.now().timestamp()` — plain floats, not `datetime`s, so
# they round-trip through the session's JSON serializer with no custom
# encoding, and compare cheaply against `time.time()` on every request with
# no DB hit).
SESSION_LOGIN_AT_KEY = "session_login_at"
SESSION_LAST_ACTIVITY_KEY = "session_last_activity"

# Throttle for rewriting `SESSION_LAST_ACTIVITY_KEY`: bumping it on literally
# every request would mark the (Redis-backed) session dirty and force a
# write on every single authenticated request. A minute of slack is
# negligible against the bounded 5min-8h idle-timeout range and keeps
# `SESSION_SAVE_EVERY_REQUEST` unnecessary (see `config/settings/base.py`).
_LAST_ACTIVITY_WRITE_THROTTLE_SECONDS = 60

# Never enforce session-expiry on the login endpoint itself. A STALE session
# cookie (e.g. one this same middleware would otherwise `_expire()`) hitting
# `POST /api/v1/auth/login` must reach `LoginView` normally -- rejecting it
# here first would turn a plain re-login attempt into a confusing
# `session-expired` 401 instead of a normal login response (400 for bad
# creds, 200 for good ones). A fresh, successful login cycles the session key
# anyway (Django's session-fixation protection in `django_login()`), so there
# is nothing useful to enforce against on this path.
_LOGIN_PATH = "/api/v1/auth/login"


class SessionTenantPreloadMiddleware:
    """Must run AFTER `SessionMiddleware` and BEFORE `AuthenticationMiddleware`
    (T0.6 finding — see rationale below). Fixes a chicken-and-egg gap between
    T0.4's default-manager wiring and T0.5's RLS that otherwise breaks EVERY
    session-authenticated request, not just login:

    `django.contrib.auth.middleware.AuthenticationMiddleware` resolves
    `request.user` lazily by loading the `User` row for the session's
    `_auth_user_id` via the auth backend's `get_user()`, which queries
    `User._default_manager` (`all_objects` — deliberately unscoped at the
    APPLICATION layer, see `apps/accounts/models.py`). But `accounts_user` is
    still RLS-protected (T0.5, `apps/tenancy/migrations/0004_rls_policies.py`),
    and RLS is enforced by Postgres itself regardless of which Django manager
    issued the query. Without the `app.current_tenant` GUC already set for
    THIS request, that lookup matches zero rows under RLS (fail-closed) —
    silently turning every session-authenticated request into an anonymous
    one, no matter how correct the session cookie is. `CurrentTenantMiddleware`
    can't fix this itself: it deliberately runs AFTER `AuthenticationMiddleware`
    (it reads `request.user.tenant_id`), so by the time it would set the GUC,
    the user lookup has already failed.

    Fix: at login, `apps.accounts.api.LoginView` writes the already-resolved
    tenant id into the session (`request.session[TENANT_SESSION_KEY]`) before
    `django.contrib.auth.login()` returns. This middleware reads that same
    value straight out of the (already server-side-decoded, Redis-backed)
    session on every later request — never from client input, satisfying R4
    exactly like `CurrentTenantMiddleware` — and pushes it into
    `tenant_context()` for the rest of the request, so by the time
    `AuthenticationMiddleware.process_request` runs its `get_user()` query,
    RLS already has the right tenant in scope.

    `CurrentTenantMiddleware` still runs afterward and re-derives the tenant
    from the now-resolved `request.user.tenant_id`; nested `tenant_context()`
    calls compose correctly (see its docstring), so `CurrentTenantMiddleware`
    remains the single source of truth for "what `request.user` says the
    tenant is" — this middleware only ever unblocks that lookup, never
    overrides it. A session with no stored tenant id (never logged in, or
    just logged out — `logout()` flushes the whole session) simply passes
    through with nothing pushed: fail-closed, identical to today's behavior.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        session = getattr(request, "session", None)
        tenant_id = session.get(TENANT_SESSION_KEY) if session is not None else None
        if tenant_id is None:
            return self.get_response(request)
        with tenant_context(tenant_id):
            return self.get_response(request)


class CurrentTenantMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tenant_id = None
        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False):
            # `user.tenant_id` — never `request.GET`/`request.headers`/body.
            tenant_id = getattr(user, "tenant_id", None)

        # tenant_context sets/clears both the contextvar (app filter) and the
        # Postgres GUC (RLS) so the two can never disagree for this request.
        with tenant_context(tenant_id):
            return self.get_response(request)


def get_session_timeout_settings(tenant_id) -> tuple[int, int]:
    """`(idle_timeout_minutes, absolute_timeout_hours)` for `tenant_id`,
    cached in Redis (`cache`, the same store as sessions — see
    `config/settings/base.py`) for 60s, explicitly invalidated by
    `apps.tenancy.api.SessionSettingsView.perform_update` on every write.
    Caps DB reads to ~1 per tenant per 60s no matter how many requests that
    tenant's members make.

    Imports `SessionSettings` lazily (module-level, not at import time of
    this file) — `apps/tenancy/models.py` doesn't import this middleware
    module, so there's no real circular-import hazard, but keeping the model
    import next to its only use here matches this file's existing style of
    importing narrowly.
    """
    from .models import SessionSettings

    cache_key = f"session_settings:{tenant_id}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    obj, _ = SessionSettings.objects.get_or_create(tenant_id=tenant_id)
    values = (obj.idle_timeout_minutes, obj.absolute_timeout_hours)
    cache.set(cache_key, values, timeout=60)
    return values


class SessionTimeoutMiddleware:
    """Enforces per-tenant idle + absolute session timeouts
    (`apps.tenancy.models.SessionSettings`), server-side, on every request —
    a per-tenant-admin-configurable replacement for the previous flat
    `SESSION_COOKIE_AGE`-only expiry (which had no idle check at all).

    **Placement is load-bearing, exactly like its neighbors in this file.**
    Registered in `MIDDLEWARE` (`config/settings/base.py`) strictly BETWEEN
    `SessionTenantPreloadMiddleware` and `AuthenticationMiddleware`:

    - It must run AFTER `SessionTenantPreloadMiddleware`, because enforcing
      the timeout means querying the RLS-protected `SessionSettings` table
      (via `get_session_timeout_settings` above), and that table is only
      visible under RLS once `SessionTenantPreloadMiddleware` has pushed the
      session's stored tenant id into `tenant_context()` — see that class's
      docstring for the full chicken-and-egg chain this unblocks. This
      middleware runs INSIDE that `with tenant_context(tenant_id):` block
      (it sits later in the same request's middleware chain, nested inside
      the `get_response(request)` call `SessionTenantPreloadMiddleware`
      wraps), so the GUC is already set here.
    - It must run BEFORE `AuthenticationMiddleware`, so an expired session is
      rejected (and flushed) before `AuthenticationMiddleware.
      process_request` spends a query resolving `request.user` from a
      session we're about to reject anyway — same reasoning
      `SessionTenantPreloadMiddleware` itself documents for its own
      placement relative to `AuthenticationMiddleware`.

    Reads `TENANT_SESSION_KEY` directly from the session (never
    `request.user`, which does not exist yet at this point in the chain) —
    identical "read straight from the already server-side-decoded,
    Redis-backed session, never client input" posture as
    `SessionTenantPreloadMiddleware` (R4).

    Exempts `_LOGIN_PATH` (`POST /api/v1/auth/login`) unconditionally, before
    any session/timestamp inspection — see that constant's docstring.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path == _LOGIN_PATH:
            return self.get_response(request)
        session = getattr(request, "session", None)
        if session is None:
            return self.get_response(request)
        tenant_id = session.get(TENANT_SESSION_KEY)
        if tenant_id is None:
            # No stored tenant id: anonymous request, or a session that never
            # logged in / already logged out. Nothing to enforce — mirrors
            # `SessionTenantPreloadMiddleware`'s own fail-closed pass-through.
            return self.get_response(request)

        now = time.time()
        login_at = session.get(SESSION_LOGIN_AT_KEY)
        last_activity = session.get(SESSION_LAST_ACTIVITY_KEY)

        if login_at is None or last_activity is None:
            # Self-heal: a session created before this feature shipped (or
            # any other reason the keys are missing) does NOT get
            # force-expired on its very first request post-deploy — that
            # would silently log out every existing user the moment this
            # ships. Stamp both to `now` and let the request proceed;
            # enforcement starts from this point forward.
            session[SESSION_LOGIN_AT_KEY] = now
            session[SESSION_LAST_ACTIVITY_KEY] = now
            return self.get_response(request)

        idle_minutes, absolute_hours = get_session_timeout_settings(tenant_id)

        if now - login_at > absolute_hours * 3600:
            return self._expire(request)
        if now - last_activity > idle_minutes * 60:
            return self._expire(request)

        if now - last_activity > _LAST_ACTIVITY_WRITE_THROTTLE_SECONDS:
            # Marks the session dirty so Django's session middleware saves it
            # at response time — deliberately NOT `SESSION_SAVE_EVERY_REQUEST`
            # (see module-level comment on the throttle constant above).
            session[SESSION_LAST_ACTIVITY_KEY] = now

        return self.get_response(request)

    def _expire(self, request):
        request.session.flush()
        # NOT `problem_response()`: that builds a DRF `Response`
        # (`SimpleTemplateResponse` subclass) which must be `.render()`-ed
        # before `.content`/`.headers["Content-Length"]` can be accessed —
        # normally done automatically by `APIView.finalize_response()`. This
        # middleware sits BEFORE `AuthenticationMiddleware`, entirely outside
        # any DRF view, so nothing would ever render it; `CommonMiddleware`
        # (earlier in `MIDDLEWARE`) reading `.content` on the way out would
        # then raise `ContentNotRenderedError`, turning every real
        # session-expiry into an HTML 500 instead of this 401. Build the
        # RFC-7807 body directly with a plain `JsonResponse`, same pattern as
        # `apps.common.errors.csrf_failure_view` (the other non-DRF-view error
        # path in this codebase).
        body = {
            "type": f"{PROBLEM_BASE}/session-expired",
            "title": "Session expired",
            "status": 401,
            "detail": "Your session has expired. Please log in again.",
        }
        return JsonResponse(body, status=401, content_type=PROBLEM_CONTENT_TYPE)
