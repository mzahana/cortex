"""Shared pytest fixtures (T0.8).

This is the drop-in point for T0.9's tenant-isolation/RBAC tests: multi-tenant
factory_boy factories go in `apps/*/tests/factories.py` (or a shared
`apps/common/tests/factories.py`).

## Two DISTINCT traps when proving Postgres RLS from pytest (read both before
## writing a T0.9 RLS test — a test that "passes" for the wrong reason is
## worse than no test)

**Trap 1 — owner-role bypass.** pytest-django's own DB connection (the `db`/
`django_db`/`transactional_db` fixtures) runs as the migration/OWNER role
(`DATABASE_URL`), which owns every table and therefore bypasses RLS
unconditionally, regardless of any GUC. Any assertion made through the
*ordinary* Django ORM/test-client connection proves nothing about RLS — it
would pass identically even if RLS were dropped entirely. This is why
`app_role_connection` below opens a SECOND connection, authenticated as the
non-superuser, RLS-subject `cortex_app` role (provisioned by
`apps.tenancy.migrations.0003_app_db_role`).

**Trap 2 — transaction-isolation false-pass (the one that actually bit an
earlier draft of this fixture).** Even with a correct RLS-subject connection,
if the row you're checking for was only INSERTed inside the *rolled-back*
transaction that the plain `db` fixture wraps every test in, a SEPARATE
connection (like `app_role_connection`) can never see it under READ COMMITTED
— not because RLS hid it, but because it was never committed. A test built
that way (`db` fixture creates the row -> `app_role_connection` queries for it
-> asserts zero rows) is a **proof of nothing**: it passes even with RLS fully
disabled, purely from ordinary transaction isolation.

**The only sound pattern: a same-committed-row, GUC-driven VISIBILITY
DIFFERENTIAL, entirely from the `app_role_connection` side.**
1. Get a row committed for real (use `transactional_db`, not `db` — see
   `insert_and_commit` below, or write it directly on `app_role_connection`
   itself and `.commit()`).
2. On `app_role_connection`, `SET`/`set_config('app.current_tenant', ...)` to
   the row's OWN tenant id -> assert the row IS visible.
3. On the SAME connection, change the GUC to a DIFFERENT tenant id (or clear
   it) -> assert the SAME row is now INVISIBLE.

Only a visibility change driven *solely* by flipping the GUC — on data that
is unquestionably committed and unquestionably still exists — isolates RLS as
the cause. See `test_rls_visibility_differential_on_committed_data` in
`apps/common/tests/test_smoke.py` for the canonical, copy-able version of this
pattern (positive case + a negative control that would fail if RLS weren't
actually enforcing the boundary).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from django.db import connection


def _app_role_dsn() -> str:
    params = connection.get_connection_params()
    return psycopg.conninfo.make_conninfo(
        host=params.get("host") or "localhost",
        port=params.get("port") or 5432,
        dbname=connection.settings_dict["NAME"],
        user=os.environ.get("APP_DB_USER", "cortex_app"),
        password=os.environ.get("APP_DB_PASSWORD", "changeme-app-db-password"),
    )


@pytest.fixture
def app_role_connection(transactional_db) -> Iterator[psycopg.Connection]:
    """A raw psycopg connection to the *same* test database pytest-django just
    migrated, authenticated as the non-superuser, RLS-subject `cortex_app` role
    (provisioned by `apps.tenancy.migrations.0003_app_db_role` — the same
    migration that runs for the owner-role test database migrate step).

    Deliberately depends on **`transactional_db`, not `db`**: `db` wraps the
    whole test in a transaction that is rolled back at the end, so anything
    written through the ordinary Django ORM connection during the test is
    NEVER actually committed and can never be seen by this separate
    connection — that mismatch is Trap 2 above, and it silently turns any RLS
    assertion built on top of it into a false-pass. `transactional_db` commits
    for real (and truncates tables afterward instead), so data created via
    the plain ORM in a test using this fixture is genuinely visible to
    other connections, letting an RLS visibility test actually test RLS.

    Also exposes `.commit()`-style writes via `insert_and_commit` (module
    function below) for tests that would rather write the row directly on
    this connection (as the `cortex_app` role itself, GUC already set) instead
    of routing through the Django ORM.
    """
    conn = psycopg.connect(_app_role_dsn())
    try:
        yield conn
    finally:
        conn.close()


def set_app_role_tenant(conn: psycopg.Connection, tenant_id: int | None) -> None:
    """Set (or clear) `app.current_tenant` on an `app_role_connection`.

    Mirrors `apps.tenancy.db.set_tenant_guc` but for a connection that isn't
    Django's `default` connection — used to drive the GUC side of the
    visibility differential from `app_role_connection` directly.
    """
    value = "" if tenant_id is None else str(tenant_id)
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('app.current_tenant', %s, false)", [value])
    conn.commit()


def insert_and_commit(sql: str, params: list[Any]) -> None:
    """Run an INSERT (or any statement) on the OWNER connection and commit it
    for real, bypassing RLS by ownership (as migrations/seeds already do) —
    a convenience for tests that want row(s) unquestionably committed without
    depending on `transactional_db` semantics for the write itself. Uses
    Django's own `default` connection, so it participates in whatever
    transactional state that connection is in; callers still need
    `transactional_db` (not `db`) for the write to survive past the current
    transaction and be visible to `app_role_connection`.
    """
    with connection.cursor() as cur:
        cur.execute(sql, params)
    connection.commit()
