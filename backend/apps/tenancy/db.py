"""Postgres RLS plumbing (T0.5) — the DB-layer backstop for R4.

This module is the single source of truth for the **session GUC convention**
that ties the application's request-scoped tenant to Postgres Row-Level
Security. Both the request middleware and any explicit caller (the T0.6 login
path, management commands, Celery tasks) set the tenant the same way, so the
app-level filter (`TenantScopedManager`, T0.4) and the RLS policies can never
disagree about "who is the current tenant".

## The convention

- A custom GUC named ``app.current_tenant`` holds the current tenant's integer
  id as text (or the empty string / unset when there is no tenant).
- Every RLS policy is exactly:

      tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::bigint

  ``current_setting(..., true)`` returns NULL when the GUC was never set;
  ``NULLIF(..., '')`` maps the "cleared" empty string to NULL as well. In both
  cases the predicate becomes ``tenant_id = NULL`` -> no rows. **Fail-closed:
  no tenant set means zero rows, never all rows.**

- The GUC is set with ``set_config(name, value, is_local => false)`` so it is
  *session*-scoped. Under the deployment's gunicorn **sync** workers there is
  exactly one in-flight request per connection, and the middleware resets the
  GUC in a ``finally`` after every request, so a value can never leak across
  requests. (A session-scoped set is used rather than ``SET LOCAL`` because the
  middleware runs *outside* the ``ATOMIC_REQUESTS`` transaction that wraps the
  view; a session set made in autocommit correctly carries into that
  transaction and is unaffected by a view rollback.)

## Who must set it

- **Authenticated requests:** ``CurrentTenantMiddleware`` sets it from
  ``request.user.tenant_id`` (never from client input) and clears it afterward.
- **The T0.6 login path** (pre-session, `request.user` is anonymous, so the
  middleware sets the GUC to '' -> RLS hides every user row): the login view
  MUST resolve the tenant first (from the login payload's tenant slug via the
  non-RLS ``tenancy_tenant`` table), call ``set_tenant_guc(tenant.id)``, and
  only then look up the ``User`` by ``(tenant, email)`` through ``all_objects``.
  Without this the `(tenant, email)` lookup returns zero rows under RLS. See the
  module note in ``apps.accounts.models``.
- **Management commands / Celery / migrations.** Any code that scopes with
  ``apps.tenancy.context.tenant_context(tenant_id)`` gets the GUC set for free
  — the context manager pushes it alongside the contextvar and restores it on
  exit — so a Celery task or command drives RLS correctly even when the worker
  connects as the RLS-subject ``cortex_app`` role. Migrations themselves run as the
  table-owner/superuser role, which bypasses RLS by ownership, so cross-tenant
  seed writes work regardless of the GUC.
"""

from __future__ import annotations

import re
from typing import Optional

# The custom Postgres GUC that carries the request's tenant id into RLS.
APP_TENANT_GUC = "app.current_tenant"

# The exact predicate every tenant-table RLS policy uses. Kept here so future
# migrations (M1+ asset tables) reuse the identical, reviewed expression rather
# than re-typing it and drifting.
RLS_PREDICATE = "tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::bigint"

# The name every tenant-isolation policy is created under (one per table).
RLS_POLICY_NAME = "tenant_isolation"

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def set_tenant_guc(tenant_id: Optional[int], using: str = "default") -> None:
    """Set (or clear) the ``app.current_tenant`` GUC on the DB connection.

    ``tenant_id=None`` clears it to the empty string, which makes every RLS
    policy match zero rows (fail-closed). Safe to call when the connecting
    role is a superuser/owner (it bypasses RLS regardless) — the set is then a
    harmless no-op with respect to visibility.
    """
    from django.db import connections

    value = "" if tenant_id is None else str(int(tenant_id))
    connection = connections[using]
    with connection.cursor() as cursor:
        # set_config lets us bind the value as a parameter (SET cannot); the
        # third arg False = session-scoped (not transaction-local).
        cursor.execute("SELECT set_config(%s, %s, false)", [APP_TENANT_GUC, value])


def _check_ident(name: str) -> str:
    if not _IDENT_RE.match(name):
        raise ValueError(f"Unsafe SQL identifier for RLS: {name!r}")
    return name


def enable_rls_sql(table: str) -> str:
    """Forward SQL: enable RLS + create the tenant-isolation policy on `table`.

    Idempotent-safe to re-run in CI: the policy is dropped first. Used by this
    migration and reusable by every future tenant-owned table's migration.
    """
    t = _check_ident(table)
    return (
        f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;\n"
        f"DROP POLICY IF EXISTS {RLS_POLICY_NAME} ON {t};\n"
        f"CREATE POLICY {RLS_POLICY_NAME} ON {t}\n"
        f"    USING ({RLS_PREDICATE})\n"
        f"    WITH CHECK ({RLS_PREDICATE});"
    )


def disable_rls_sql(table: str) -> str:
    """Reverse SQL: drop the policy + disable RLS on `table`."""
    t = _check_ident(table)
    return (
        f"DROP POLICY IF EXISTS {RLS_POLICY_NAME} ON {t};\n"
        f"ALTER TABLE {t} DISABLE ROW LEVEL SECURITY;"
    )
