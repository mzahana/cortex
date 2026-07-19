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

from .context import tenant_context

# Session key written by `apps.accounts.api.LoginView` right after a
# successful login, read back by `SessionTenantPreloadMiddleware` below.
TENANT_SESSION_KEY = "tenant_id"


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
