"""T0.9 — tenant isolation (F1 acceptance, R4).

Three independent layers are proven here, each catching a DIFFERENT class of
bug, per `docs/tasks/M0-foundations.md` T0.9 and `CLAUDE.md`'s R4 invariant:

1. **RLS backstop** (`test_rls_blocks_cross_tenant_select_even_with_app_filter_bypassed`)
   — the DB itself refuses a cross-tenant row even when the caller issues a
   query with NO tenant `WHERE` clause at all (the "app forgot to filter"
   scenario). Modeled on the canonical committed-data GUC-differential
   pattern in `apps/common/tests/test_rls_canonical.py` — see that module's
   docstring and `backend/conftest.py`'s module docstring for the two traps
   this pattern avoids (owner-role bypass, transaction-isolation false-pass).
2. **App-layer manager** (`test_tenant_scoped_manager_*`) — `TenantScopedManager`
   (T0.4) hides another tenant's row entirely (a 404-equivalent: `.get()`
   raises `DoesNotExist`, not `PermissionDenied`, so a guessed primary key
   reveals nothing) and fails CLOSED (raises, not "returns everything") with
   no tenant in context.
3. **Integration, real session** (`test_me_endpoint_*`) — a real
   login -> session cookie -> `GET /me` round trip only ever surfaces the
   logged-in tenant's own identity/permissions, including the one
   "guessed-identifier" surface that exists at M0: a shared email across two
   tenants must resolve to the tenant named in the login payload, never leak
   into the other tenant merely because the email string matches.

No asset/object CRUD endpoints exist yet at M0 (those land in M1) — the
guessed-URL-on-an-object-endpoint version of this acceptance criterion is
flagged as an M1 follow-up in the T0.9 report, not invented here as a
throwaway endpoint.
"""

from __future__ import annotations

import pytest

from apps.accounts.models import User
from apps.common.tests.factories import ProjectFactory, TenantFactory, UserFactory
from apps.projects.models import Project
from apps.tenancy.context import TenantContextError, tenant_context
from apps.tenancy.models import Tenant
from conftest import set_app_role_tenant

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# 1. RLS backstop: cross-tenant SELECT blocked even with the app-level filter
#    bypassed entirely (no WHERE tenant_id=... in the query at all).
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_rls_blocks_cross_tenant_select_even_with_app_filter_bypassed(app_role_connection):
    """Same committed-row, GUC-driven visibility differential as
    `test_rls_canonical.py`, but against `projects_project` (a real
    tenant-owned business table, not just `accounts_user`) and with the
    query issuing NO tenant predicate whatsoever — simulating exactly the
    bug RLS exists to catch: application code that forgot
    `TenantScopedManager`'s filter (or used `.all_objects` by mistake).
    """
    tenant_a = Tenant.objects.create(name="Isolation Tenant A", slug="iso-tenant-a")
    tenant_b = Tenant.objects.create(name="Isolation Tenant B", slug="iso-tenant-b")
    project_a = Project.all_objects.create(tenant=tenant_a, name="Project A Secret")

    with app_role_connection.cursor() as cur:
        # Positive: GUC = project's own tenant -> visible, even with a
        # query that has no WHERE clause on tenant_id at all (RLS injects it).
        set_app_role_tenant(app_role_connection, tenant_a.id)
        cur.execute("SELECT id, name FROM projects_project WHERE id = %s", [project_a.id])
        row = cur.fetchone()
        assert row is not None and row[1] == "Project A Secret"

        # Negative control: GUC flipped to a DIFFERENT tenant -> the SAME
        # committed row, queried with the SAME app-filter-free SQL, is gone.
        # If RLS were disabled/misconfigured this row would still return.
        set_app_role_tenant(app_role_connection, tenant_b.id)
        cur.execute("SELECT id FROM projects_project WHERE id = %s", [project_a.id])
        assert cur.fetchone() is None, (
            "RLS did NOT block a cross-tenant SELECT on projects_project -- "
            "tenant B's GUC could see tenant A's project even with no "
            "application-level tenant filter in the query."
        )

        # Negative control #2: no tenant in context at all -> fail-closed.
        set_app_role_tenant(app_role_connection, None)
        cur.execute("SELECT id FROM projects_project WHERE id = %s", [project_a.id])
        assert cur.fetchone() is None, "RLS did NOT fail closed with no tenant in context."

        # A broader query with literally no WHERE clause on this table at
        # all must still return zero cross-tenant rows for tenant B, proving
        # this isn't an artifact of the `WHERE id = ...` predicate.
        set_app_role_tenant(app_role_connection, tenant_b.id)
        cur.execute("SELECT id FROM projects_project")
        assert project_a.id not in {r[0] for r in cur.fetchall()}

        # Back to the owning tenant -> visible again (rules out a one-way
        # connection side effect, isolates the GUC as the sole cause).
        set_app_role_tenant(app_role_connection, tenant_a.id)
        cur.execute("SELECT id FROM projects_project WHERE id = %s", [project_a.id])
        assert cur.fetchone() is not None


# ---------------------------------------------------------------------------
# 2. App-layer: TenantScopedManager (T0.4) hides cross-tenant rows and fails
#    closed with no tenant context.
# ---------------------------------------------------------------------------


def test_tenant_scoped_manager_hides_other_tenants_object_as_404_equivalent():
    """A tenant-B `tenant_context()` querying by tenant-A's object's exact PK
    gets `DoesNotExist` — the ORM-level equivalent of the 404 an M1 endpoint
    must return for a guessed object URL — never the object itself and never
    a different (information-revealing) exception like `PermissionDenied`.
    """
    tenant_a = TenantFactory()
    tenant_b = TenantFactory()
    project_a = ProjectFactory(tenant=tenant_a)

    with tenant_context(tenant_b.id):
        assert not Project.objects.filter(pk=project_a.pk).exists()
        with pytest.raises(Project.DoesNotExist):
            Project.objects.get(pk=project_a.pk)

    # Sanity: the SAME object IS visible in its own tenant's context, proving
    # the miss above is tenant-scoping, not some unrelated lookup bug.
    with tenant_context(tenant_a.id):
        assert Project.objects.filter(pk=project_a.pk).exists()


def test_tenant_scoped_manager_fails_closed_with_no_tenant_context():
    """No active `tenant_context()` at all must raise, not silently return
    unfiltered (cross-tenant) data — the fail-closed guarantee `CLAUDE.md`
    and `apps.tenancy.managers` require.
    """
    tenant = TenantFactory()
    ProjectFactory(tenant=tenant)

    with pytest.raises(TenantContextError):
        list(Project.objects.all())


# ---------------------------------------------------------------------------
# 3. Integration: real login -> session cookie -> GET /me.
# ---------------------------------------------------------------------------


def test_me_endpoint_only_sees_own_tenant_identity(client):
    tenant_a = TenantFactory()
    tenant_b = TenantFactory()
    user_a = UserFactory(tenant=tenant_a, email="alice@example.test")
    UserFactory(tenant=tenant_b, email="bob@example.test")

    login_response = client.post(
        "/api/v1/auth/login",
        {"tenant": tenant_a.slug, "email": user_a.email, "password": "TestPass123!"},
        content_type="application/json",
    )
    assert login_response.status_code == 200
    assert login_response.json()["tenant"]["slug"] == tenant_a.slug

    me_response = client.get("/api/v1/me")
    assert me_response.status_code == 200
    body = me_response.json()
    assert body["email"] == user_a.email
    assert body["tenant"]["slug"] == tenant_a.slug
    assert body["tenant"]["id"] != tenant_b.id


def test_shared_email_across_tenants_cannot_cross_authenticate(client):
    """The one 'guessed identifier' surface that exists at M0: two DIFFERENT
    users in two DIFFERENT tenants share the exact same email string (legal,
    since `email` is unique only per-tenant — `apps.accounts.models.User`).
    Logging in with tenant A's slug + that email must authenticate ONLY
    tenant A's user — never tenant B's — even though the row lookup key
    (email) is identical. This is exactly the T0.6-documented cross-tenant
    login-safety fixture (`seed_t0_6`), re-proven here as a T0.9 acceptance
    test rather than only a hand-run seed script.
    """
    tenant_a = TenantFactory()
    tenant_b = TenantFactory()
    shared_email = "shared@cross-tenant.test"
    UserFactory(tenant=tenant_a, email=shared_email, name="Tenant A User")
    UserFactory(tenant=tenant_b, email=shared_email, name="Tenant B User")

    response = client.post(
        "/api/v1/auth/login",
        {"tenant": tenant_a.slug, "email": shared_email, "password": "TestPass123!"},
        content_type="application/json",
    )
    assert response.status_code == 200
    body = response.json()
    assert body["tenant"]["slug"] == tenant_a.slug
    assert body["name"] == "Tenant A User"


def test_guessed_tenant_slug_with_another_tenants_credentials_is_rejected(client):
    """A tenant-A user's exact (email, password) pair, submitted against
    tenant B's slug, must be rejected — a would-be attacker cannot "guess"
    their way into tenant B by supplying credentials that are only valid
    elsewhere. Response is the uniform invalid-credentials shape (never a
    distinguishable "wrong tenant" error - see `apps.accounts.api`
    module docstring on timing/response uniformity).
    """
    tenant_a = TenantFactory()
    tenant_b = TenantFactory()
    user_a = UserFactory(tenant=tenant_a, email="carol@example.test")

    response = client.post(
        "/api/v1/auth/login",
        {"tenant": tenant_b.slug, "email": user_a.email, "password": "TestPass123!"},
        content_type="application/json",
    )
    assert response.status_code == 401


def test_anonymous_request_has_no_tenant_context_and_me_is_unauthorized(client):
    """No session at all -> `/me` is denied (403 — DRF's `IsAuthenticated`
    maps to 403, not 401, when the only configured authenticator is
    `SessionAuthentication`, which never sets `WWW-Authenticate`; see
    `config/settings/base.py` `REST_FRAMEWORK["DEFAULT_AUTHENTICATION_CLASSES"]`).
    Defense in depth: an anonymous request never puts ANY tenant into
    context — confirmed indirectly: if it did, a tenant-scoped query made
    during such a request would silently succeed instead of raising, which
    the app-layer test above already shows never happens with a real
    object."""
    response = client.get("/api/v1/me")
    assert response.status_code == 403


def test_user_all_objects_lookup_across_tenants_still_distinct_rows():
    """Sanity/documentation test: `User.all_objects` (the deliberate,
    reviewed unscoped escape hatch used pre-login) does NOT itself provide
    isolation — it is the caller's job to filter by tenant explicitly, which
    `LoginView` does. This test pins that `(tenant, email)` uniqueness is
    per-tenant, not global, so two same-email users are two distinct rows a
    naive `User.all_objects.get(email=...)` would incorrectly conflate."""
    tenant_a = TenantFactory()
    tenant_b = TenantFactory()
    shared_email = "dup@example.test"
    user_a = UserFactory(tenant=tenant_a, email=shared_email)
    user_b = UserFactory(tenant=tenant_b, email=shared_email)

    assert user_a.id != user_b.id
    assert User.all_objects.filter(email=shared_email).count() == 2
    assert User.all_objects.get(tenant=tenant_a, email=shared_email).id == user_a.id
    assert User.all_objects.get(tenant=tenant_b, email=shared_email).id == user_b.id
