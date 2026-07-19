"""The single source of truth for "what is the current tenant" (T0.4).

This is a `contextvars.ContextVar`, not a thread-local dict keyed by request,
so it behaves correctly under both WSGI (sync, thread-per-request via
gunicorn `sync` workers) and any future ASGI/async code path.

**Critical invariant (R4):** nothing in this module ever reads the tenant from
request query params, headers, or body. The only writer is
`apps.tenancy.middleware.CurrentTenantMiddleware`, which derives the tenant
from `request.user` — itself populated by Django's session machinery from the
signed session cookie, never from client-supplied tenant data. Tests/scripts
that need to run outside a request use the `tenant_context()` context manager
explicitly (e.g. management commands, Celery tasks that already know the
tenant of the object they're processing).

**T0.5 — one entry point, two mechanisms kept in lockstep.** `tenant_context()`
sets BOTH the app-level contextvar (read by `TenantScopedManager`) AND the
Postgres session GUC `app.current_tenant` (read by RLS, see `apps.tenancy.db`).
This is what makes the app-filter scope and the RLS scope agree on *every*
path, not just HTTP requests: Celery tasks, management commands and seeds that
scope with `tenant_context()` now hit RLS with the correct tenant instead of an
unset GUC (which, once the worker connects as the RLS-subject `lms_app` role,
would otherwise be fail-closed to zero rows and silently break the task). The
HTTP middleware simply enters `tenant_context()` and inherits the same
behavior. The GUC is restored to the *outer* scope's tenant on exit (not blanket
cleared), so nested `tenant_context()` calls compose correctly.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Iterator, Optional

_current_tenant_id: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "current_tenant_id", default=None
)


class TenantContextError(RuntimeError):
    """Raised when tenant-scoped ORM access is attempted with no tenant set.

    This is the fail-closed behavior required by docs/rbac.md and
    CLAUDE.md: a missing tenant context must never silently return
    unfiltered (cross-tenant) data.
    """


def get_current_tenant_id() -> Optional[int]:
    """Return the tenant id for the current request/task, or `None` if unset."""
    return _current_tenant_id.get()


def set_current_tenant_id(tenant_id: Optional[int]) -> contextvars.Token:
    """Low-level setter. Prefer `tenant_context()` outside of middleware."""
    return _current_tenant_id.set(tenant_id)


def reset_current_tenant_id(token: contextvars.Token) -> None:
    _current_tenant_id.reset(token)


@contextmanager
def tenant_context(tenant_id: Optional[int]) -> Iterator[None]:
    """Explicitly scope a block of code to `tenant_id`.

    Used by: the tenant middleware (request scope), management commands,
    Celery tasks, tests, and the seed migration/signals — anywhere tenant
    scoping is needed outside of an authenticated HTTP request. Never used to
    let client input choose the tenant.

    Sets both the app-level contextvar and the Postgres `app.current_tenant`
    session GUC that RLS reads, and on exit restores the GUC to whatever tenant
    is now current (the outer scope after nesting, or cleared at the top level)
    — mirroring the contextvar stack exactly. `set_config(..., is_local=false)`
    is used so the value is session-scoped and is observed by subsequent ORM
    queries on the same connection whether or not a transaction is open (the
    common autocommit case for a freshly-started Celery task).

    Note: entering/leaving issues a tiny `SELECT set_config(...)` and therefore
    needs a usable DB connection — which every legitimate caller already has,
    since the whole point is to scope DB access. It sets the GUC on the
    ``default`` connection.
    """
    # Local import to avoid any import-time coupling and to keep the DB access
    # lazy (db.set_tenant_guc imports django.db.connections on call).
    from .db import set_tenant_guc

    token = set_current_tenant_id(tenant_id)
    guc_pushed = False
    try:
        set_tenant_guc(tenant_id)
        guc_pushed = True
        yield
    finally:
        reset_current_tenant_id(token)
        if guc_pushed:
            # Restore the GUC to the (now-restored) outer tenant so nested
            # scopes compose: exiting an inner tenant_context returns RLS to the
            # outer tenant, not to "no tenant". At the top level this clears it
            # to '' (fail-closed).
            set_tenant_guc(get_current_tenant_id())
