"""T1.9 — closes the M0 sign-off's carried CI blind-spot: **no automated test
had ever driven a real Django HTTP request whose DB connection is the
non-superuser, RLS-subject `cortex_app` role.**

Every prior RLS proof in this codebase (`apps.common.tests.test_smoke`,
`apps.tenancy.tests.test_tenant_isolation`, `apps.common.tests.
test_rls_canonical`) uses `backend/conftest.py`'s `app_role_connection` — a
SECOND, raw `psycopg` connection opened purely to run hand-written SQL as
`cortex_app`. That proves the ROLE/POLICY plumbing is correct in isolation,
but every actual HTTP-cycle test in the suite (`client.post("/api/v1/auth/
login", ...)`, `client.get("/api/v1/me")`, every asset endpoint test) runs
through pytest-django's own `default` connection, which is the migration/
OWNER role (`docs/tasks`/`conftest.py`'s "Trap 1") — a superuser that bypasses
RLS by ownership regardless of the GUC. So `SessionTenantPreloadMiddleware`
existing and doing the right thing, and RLS actually backstopping a real
request in production, had only ever been proven by *reasoning* (the
docstrings in `apps.tenancy.middleware`/`apps.tenancy.db`), never exercised
end-to-end by a test that would fail if that reasoning were wrong.

## How this connects as `cortex_app`

`_as_cortex_app_role()` below temporarily overwrites Django's **`default`**
connection's own `settings_dict["USER"]`/`["PASSWORD"]` (the exact alias
`request.user`/the ORM/every view actually queries through — not a second,
side-channel connection) to `APP_DB_USER`/`APP_DB_PASSWORD` (env, same
convention `conftest.py::_app_role_dsn` and `docker-compose.yml`'s `web`/
`worker`/`beat` services use for `APP_DATABASE_URL`), closes the currently-open
owner-role connection so the next query is forced to *reconnect* under the
new credentials, and restores the owner credentials (+ closes again) in a
`finally` — restoring BEFORE `transactional_db`'s own teardown runs (which
issues `TRUNCATE ...`, a privilege `cortex_app` was deliberately never granted
— see `apps.tenancy.migrations.0003_app_db_role`), because a context manager
entered inside the test body finalizes before the enclosing fixture's
teardown.

Setup (tenant/user/asset creation via factories) happens **before** entering
`_as_cortex_app_role()`, still on the owner connection — this must stay this
way: `cortex_app` has no way to satisfy RLS's `WITH CHECK` on an `INSERT` with
no `app.current_tenant` GUC set yet (the row would violate the policy, not
merely a plain permission error), and real request handling never has the
app itself insert rows without a request-scoped tenant in context either.
`@pytest.mark.django_db(transaction=True)` (equivalent to the `transactional_db`
fixture) is required, not the plain `db` fixture: setup must be genuinely
COMMITTED before the connection closes/reopens, or a rolled-back-at-teardown
transaction's rows would simply vanish when the connection is torn down and
re-established under different credentials.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

import pytest
from django.db import connections

from apps.assets.models import Asset
from apps.catalog.models import Category
from apps.common.tests.factories import DEFAULT_TEST_PASSWORD, TenantFactory, UserFactory
from apps.tenancy.context import tenant_context


def _app_role_credentials() -> tuple[str, str]:
    return (
        os.environ.get("APP_DB_USER", "cortex_app"),
        os.environ.get("APP_DB_PASSWORD", "changeme-app-db-password"),
    )


@contextmanager
def _as_cortex_app_role() -> Iterator[None]:
    """Swap Django's `default` connection to the `cortex_app` role for
    everything executed inside this block, then restore the owner role.

    See module docstring for the full rationale/ordering constraints.
    """
    conn = connections["default"]
    original_user = conn.settings_dict["USER"]
    original_password = conn.settings_dict["PASSWORD"]
    app_user, app_password = _app_role_credentials()

    conn.close()  # force the next query to open a brand-new connection
    conn.settings_dict["USER"] = app_user
    conn.settings_dict["PASSWORD"] = app_password
    try:
        yield
    finally:
        conn.close()
        conn.settings_dict["USER"] = original_user
        conn.settings_dict["PASSWORD"] = original_password


@pytest.mark.django_db(transaction=True)
def test_login_me_and_asset_list_run_end_to_end_as_cortex_app(client):
    """The full chain — login -> session cookie -> `GET /me` -> `GET
    /api/v1/assets/` -- driven ENTIRELY through the `cortex_app` connection,
    proving `SessionTenantPreloadMiddleware` + `CurrentTenantMiddleware` +
    the GUC + RLS all cooperate correctly at runtime, not just in theory:

    if `SessionTenantPreloadMiddleware` did not exist (or set the GUC too
    late), `AuthenticationMiddleware`'s own `get_user()` query for the
    session's user would return zero rows under RLS on this non-superuser
    connection, and login/`/me` would 401/403 instead of succeeding — this
    test would have caught EXACTLY the regression that middleware's
    docstring says it fixes, run against the real role.
    """
    # --- Setup on the owner connection (still cortex, not yet swapped) -----
    tenant_a = TenantFactory()
    tenant_b = TenantFactory()
    user_a = UserFactory(tenant=tenant_a, email="alice@cortex-app-rls.test")
    UserFactory(tenant=tenant_b, email="bob@cortex-app-rls.test")

    with tenant_context(tenant_a.id):
        category_a = Category.all_objects.create(tenant=tenant_a, name="Compute")
        asset_a = Asset.all_objects.create(
            tenant=tenant_a, category=category_a, name="Tenant A's GPU Box"
        )
    with tenant_context(tenant_b.id):
        category_b = Category.all_objects.create(tenant=tenant_b, name="Compute")
        Asset.all_objects.create(tenant=tenant_b, category=category_b, name="Tenant B's Secret")

    app_user, _ = _app_role_credentials()

    with _as_cortex_app_role():
        conn = connections["default"]

        # Sanity: this connection really is the non-superuser, NOBYPASSRLS
        # `cortex_app` role — not a superuser that would make every assertion
        # below pass trivially regardless of RLS (mirrors the same check
        # `apps.common.tests.test_smoke::test_app_role_connection_is_rls_subject`
        # does for the raw `app_role_connection`, now against the ACTUAL
        # connection Django's ORM/views use).
        with conn.cursor() as cur:
            cur.execute("SELECT current_user")
            assert cur.fetchone()[0] == app_user
            cur.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            rolsuper, rolbypassrls = cur.fetchone()
            assert rolsuper is False
            assert rolbypassrls is False

        # --- Real HTTP login, over the cortex_app connection -----------------
        login_response = client.post(
            "/api/v1/auth/login",
            {
                "tenant": tenant_a.slug,
                "email": user_a.email,
                "password": DEFAULT_TEST_PASSWORD,
            },
            content_type="application/json",
        )
        assert login_response.status_code == 200, login_response.content
        assert login_response.json()["tenant"]["slug"] == tenant_a.slug

        # --- GET /me over the SAME connection/session -------------------------
        me_response = client.get("/api/v1/me")
        assert me_response.status_code == 200, me_response.content
        me_body = me_response.json()
        assert me_body["email"] == user_a.email
        assert me_body["tenant"]["slug"] == tenant_a.slug
        assert me_body["tenant"]["id"] != tenant_b.id

        # --- Tenant-scoped asset list, also over cortex_app: only tenant A's
        # asset is visible, tenant B's never leaks -- app-level filter AND
        # RLS agreeing, both running as the real runtime role. ---
        list_response = client.get("/api/v1/assets/")
        assert list_response.status_code == 200, list_response.content
        names = [a["name"] for a in list_response.json()["results"]]
        assert names == ["Tenant A's GPU Box"]

        # Guessed cross-tenant object URL -> 404, not a leak, still under
        # cortex_app (R4, same runtime role as production).
        detail_response = client.get(f"/api/v1/assets/{asset_a.id}/")
        assert detail_response.status_code == 200
        with tenant_context(tenant_b.id):
            # (Owner-side lookup just to get tenant B's asset id conveniently;
            # doesn't touch the cortex_app connection.)
            other_asset_id = Asset.objects.get(name="Tenant B's Secret").id
        cross_tenant_response = client.get(f"/api/v1/assets/{other_asset_id}/")
        assert cross_tenant_response.status_code == 404

        client.post("/api/v1/auth/logout")

        # --- Negative control: the RLS backstop, proven on THIS EXACT
        # connection right after the real request cycle that just worked.
        # `CurrentTenantMiddleware`/`SessionTenantPreloadMiddleware` clear the
        # GUC in their `finally` after every request/on logout, so a raw
        # SELECT issued directly on this same connection with no GUC set
        # must now see ZERO rows for a table that unquestionably has rows
        # (this connection just successfully listed one moments ago) --
        # isolating RLS itself (not e.g. session expiry or app-level
        # filtering) as what is protecting this connection between requests.
        with conn.cursor() as cur:
            cur.execute("SELECT current_setting('app.current_tenant', true)")
            assert cur.fetchone()[0] in (None, "")
            cur.execute("SELECT id FROM assets_asset WHERE tenant_id = %s", [tenant_a.id])
            assert cur.fetchone() is None, (
                "A raw SELECT on the cortex_app connection saw tenant A's asset "
                "with no app.current_tenant GUC set -- RLS is not actually "
                "backstopping this connection between requests."
            )


@pytest.mark.django_db(transaction=True)
def test_anonymous_request_over_cortex_app_has_no_tenant_leak(client):
    """No session at all, still over the `cortex_app` connection: `/me` is
    denied and no tenant data is reachable -- the fail-closed baseline this
    role must never regress from."""
    tenant = TenantFactory()
    with tenant_context(tenant.id):
        category = Category.all_objects.create(tenant=tenant, name="Compute")
        Asset.all_objects.create(tenant=tenant, category=category, name="Should Never Leak")

    with _as_cortex_app_role():
        me_response = client.get("/api/v1/me")
        assert me_response.status_code == 403

        list_response = client.get("/api/v1/assets/")
        # No session -> IsAuthenticated denies before any tenant-scoped query
        # even runs; still asserted here so a future permission-class change
        # that accidentally widened this would be caught against the real
        # cortex_app connection too.
        assert list_response.status_code == 403
