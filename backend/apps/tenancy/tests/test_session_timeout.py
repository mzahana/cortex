"""Acceptance tests for the per-tenant idle/absolute session timeout feature:
`SessionTimeoutMiddleware`, `SessionSettings`, and
`GET/PATCH /api/v1/tenancy/session-settings`.

Structured like `apps.notifications.tests.test_email_settings_api` (the
closest precedent — a tenant-wide, admin-gated singleton settings resource),
plus middleware-level tests for the actual timeout enforcement, which has no
existing precedent in this app.

Covers:
- idle expiry, absolute expiry (independent of activity), within-bounds pass,
  throttled `last_activity` write, missing-keys self-heal, per-tenant
  isolation of the bounds check;
- RLS backstop on `tenancy_session_settings` itself;
- RBAC gate on the endpoint (403 without `tenant.manage`);
- bounds validation (400s);
- audit entry correctness;
- cache invalidation on PATCH;
- login stamps both timestamps.
"""

from __future__ import annotations

import json
import time

import pytest
from django.core.cache import cache
from django.test import Client

from apps.audit.models import AuditLog
from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
    TenantFactory,
    UserFactory,
    upgrade_tenant_wide_role,
)
from apps.rbac.permission_keys import ROLE_ADMIN
from apps.tenancy.context import tenant_context
from apps.tenancy.middleware import (
    SESSION_LAST_ACTIVITY_KEY,
    SESSION_LOGIN_AT_KEY,
    TENANT_SESSION_KEY,
)
from apps.tenancy.models import SessionSettings
from conftest import set_app_role_tenant

pytestmark = pytest.mark.django_db

URL = "/api/v1/tenancy/session-settings"


def _login(client, tenant, user):
    response = client.post(
        "/api/v1/auth/login",
        {"tenant": tenant.slug, "email": user.email, "password": DEFAULT_TEST_PASSWORD},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return response


def _admin(tenant):
    admin = UserFactory(tenant=tenant)
    upgrade_tenant_wide_role(admin, ROLE_ADMIN)
    return admin


def _set_session_key(client, key, value):
    session = client.session
    session[key] = value
    session.save()


class TestSessionTimeoutEnforcement:
    def test_idle_expiry_returns_401_and_flushes_session(self, client):
        tenant = TenantFactory()
        user = UserFactory(tenant=tenant)
        _login(client, tenant, user)

        # Confirm baseline works before rewinding.
        ok = client.get("/api/v1/me")
        assert ok.status_code == 200

        now = time.time()
        _set_session_key(client, SESSION_LOGIN_AT_KEY, now - 30)  # well within absolute bound
        # Default idle bound is 60 minutes; push last_activity far past it.
        _set_session_key(client, SESSION_LAST_ACTIVITY_KEY, now - (61 * 60))

        resp = client.get("/api/v1/me")
        assert resp.status_code == 401, resp.content
        assert resp["Content-Type"] == "application/problem+json"
        body = resp.json()
        assert "session-expired" in body["type"]

        # Session was flushed: a follow-up request on the same cookie is now
        # anonymous, not merely "still expired".
        follow_up = client.get("/api/v1/me")
        assert follow_up.status_code == 403  # IsAuthenticated on an anonymous request

    def test_absolute_expiry_fires_even_with_fresh_activity(self, client):
        tenant = TenantFactory()
        user = UserFactory(tenant=tenant)
        _login(client, tenant, user)

        now = time.time()
        # last_activity is fresh (just now), but login_at is far past the
        # default 24h absolute bound.
        _set_session_key(client, SESSION_LAST_ACTIVITY_KEY, now)
        _set_session_key(client, SESSION_LOGIN_AT_KEY, now - (25 * 3600))

        resp = client.get("/api/v1/me")
        assert resp.status_code == 401, resp.content
        body = resp.json()
        assert "session-expired" in body["type"]

    def test_within_bounds_passes_and_bumps_last_activity(self, client):
        tenant = TenantFactory()
        user = UserFactory(tenant=tenant)
        _login(client, tenant, user)

        now = time.time()
        # Stale enough to exceed the 60s write-throttle, but well inside the
        # idle/absolute bounds.
        stale_activity = now - 120
        _set_session_key(client, SESSION_LAST_ACTIVITY_KEY, stale_activity)
        _set_session_key(client, SESSION_LOGIN_AT_KEY, now - 3600)

        resp = client.get("/api/v1/me")
        assert resp.status_code == 200, resp.content
        assert client.session[SESSION_LAST_ACTIVITY_KEY] > stale_activity

    def test_throttled_write_does_not_rewrite_within_60s(self, client):
        tenant = TenantFactory()
        user = UserFactory(tenant=tenant)
        _login(client, tenant, user)

        now = time.time()
        recent_activity = now - 10  # inside the 60s throttle window
        _set_session_key(client, SESSION_LAST_ACTIVITY_KEY, recent_activity)
        _set_session_key(client, SESSION_LOGIN_AT_KEY, now - 3600)

        resp = client.get("/api/v1/me")
        assert resp.status_code == 200, resp.content
        assert client.session[SESSION_LAST_ACTIVITY_KEY] == recent_activity

    def test_missing_keys_self_heal_instead_of_force_expiring(self, client):
        tenant = TenantFactory()
        user = UserFactory(tenant=tenant)
        _login(client, tenant, user)

        # Simulate a pre-feature session: strip both new keys but keep the
        # tenant id (which existed before this feature too).
        session = client.session
        del session[SESSION_LOGIN_AT_KEY]
        del session[SESSION_LAST_ACTIVITY_KEY]
        session.save()
        assert SESSION_LOGIN_AT_KEY not in client.session
        assert SESSION_LAST_ACTIVITY_KEY not in client.session

        resp = client.get("/api/v1/me")
        assert resp.status_code == 200, resp.content
        assert SESSION_LOGIN_AT_KEY in client.session
        assert SESSION_LAST_ACTIVITY_KEY in client.session

    def test_per_tenant_isolation_of_bounds_check(self, client):
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        admin_a = _admin(tenant_a)
        user_b = UserFactory(tenant=tenant_b)

        with tenant_context(tenant_a.id):
            SessionSettings.objects.update_or_create(
                tenant=tenant_a, defaults={"idle_timeout_minutes": 5, "absolute_timeout_hours": 24}
            )
        with tenant_context(tenant_b.id):
            SessionSettings.objects.update_or_create(
                tenant=tenant_b,
                defaults={"idle_timeout_minutes": 480, "absolute_timeout_hours": 24},
            )

        # Tenant A: idle timeout is 5 minutes -- 10 minutes of inactivity
        # must expire it.
        client_a = Client()
        _login(client_a, tenant_a, admin_a)
        now = time.time()
        session_a = client_a.session
        session_a[SESSION_LAST_ACTIVITY_KEY] = now - (10 * 60)
        session_a[SESSION_LOGIN_AT_KEY] = now - 3600
        session_a.save()
        resp_a = client_a.get("/api/v1/me")
        assert resp_a.status_code == 401, resp_a.content

        # Tenant B: idle timeout is 480 minutes -- the SAME 10 minutes of
        # inactivity must NOT expire it -- proves the check reads its own
        # tenant's configured value, not tenant A's (or a shared default).
        client_b = Client()
        _login(client_b, tenant_b, user_b)
        session_b = client_b.session
        session_b[SESSION_LAST_ACTIVITY_KEY] = now - (10 * 60)
        session_b[SESSION_LOGIN_AT_KEY] = now - 3600
        session_b.save()
        resp_b = client_b.get("/api/v1/me")
        assert resp_b.status_code == 200, resp_b.content


class TestSessionSettingsRLS:
    @pytest.mark.django_db(transaction=True)
    def test_rls_blocks_cross_tenant_select_even_with_app_filter_bypassed(
        self, app_role_connection
    ):
        from apps.tenancy.models import Tenant

        tenant_a = Tenant.objects.create(name="Session Tenant A", slug="sess-tenant-a")
        tenant_b = Tenant.objects.create(name="Session Tenant B", slug="sess-tenant-b")
        row_a = SessionSettings.all_objects.create(
            tenant=tenant_a, idle_timeout_minutes=42, absolute_timeout_hours=12
        )

        with app_role_connection.cursor() as cur:
            set_app_role_tenant(app_role_connection, tenant_a.id)
            cur.execute(
                "SELECT idle_timeout_minutes FROM tenancy_session_settings WHERE id = %s",
                [row_a.id],
            )
            assert cur.fetchone() == (42,)

            set_app_role_tenant(app_role_connection, tenant_b.id)
            cur.execute("SELECT id FROM tenancy_session_settings WHERE id = %s", [row_a.id])
            assert cur.fetchone() is None, (
                "RLS did NOT block a cross-tenant SELECT on "
                "tenancy_session_settings -- tenant B's GUC could see tenant "
                "A's row even with no application-level tenant filter."
            )

            set_app_role_tenant(app_role_connection, None)
            cur.execute("SELECT id FROM tenancy_session_settings WHERE id = %s", [row_a.id])
            assert cur.fetchone() is None, "RLS did NOT fail closed with no tenant in context."

    def test_app_layer_tenant_scoped_manager_hides_other_tenants_row(self):
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        row_a = SessionSettings.all_objects.create(tenant=tenant_a)

        with tenant_context(tenant_b.id):
            assert not SessionSettings.objects.filter(pk=row_a.pk).exists()

        with tenant_context(tenant_a.id):
            assert SessionSettings.objects.filter(pk=row_a.pk).exists()


class TestSessionSettingsRBAC:
    def test_member_gets_403_on_get_and_patch(self, client):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)  # default tenant-wide Member
        _login(client, tenant, member)

        get_resp = client.get(URL)
        assert get_resp.status_code == 403, get_resp.content

        patch_resp = client.patch(
            URL,
            data=json.dumps({"idle_timeout_minutes": 30}),
            content_type="application/json",
        )
        assert patch_resp.status_code == 403, patch_resp.content

    def test_admin_can_get_and_patch(self, client):
        tenant = TenantFactory()
        admin = _admin(tenant)
        _login(client, tenant, admin)

        get_resp = client.get(URL)
        assert get_resp.status_code == 200, get_resp.content

        patch_resp = client.patch(
            URL,
            data=json.dumps({"idle_timeout_minutes": 30}),
            content_type="application/json",
        )
        assert patch_resp.status_code == 200, patch_resp.content
        assert patch_resp.json()["idle_timeout_minutes"] == 30


class TestSessionSettingsBoundsValidation:
    @pytest.mark.parametrize(
        "payload",
        [
            {"idle_timeout_minutes": 4},
            {"idle_timeout_minutes": 481},
            {"absolute_timeout_hours": 0},
            {"absolute_timeout_hours": 169},
        ],
    )
    def test_out_of_bounds_patch_is_rejected(self, client, payload):
        tenant = TenantFactory()
        admin = _admin(tenant)
        _login(client, tenant, admin)

        resp = client.patch(URL, data=json.dumps(payload), content_type="application/json")
        assert resp.status_code == 400, resp.content

    @pytest.mark.parametrize(
        "payload",
        [
            {"idle_timeout_minutes": 5},
            {"idle_timeout_minutes": 480},
            {"absolute_timeout_hours": 1},
            {"absolute_timeout_hours": 168},
        ],
    )
    def test_in_bounds_patch_is_accepted(self, client, payload):
        tenant = TenantFactory()
        admin = _admin(tenant)
        _login(client, tenant, admin)

        resp = client.patch(URL, data=json.dumps(payload), content_type="application/json")
        assert resp.status_code == 200, resp.content


class TestSessionSettingsAudit:
    def test_patch_writes_correct_audit_before_after(self, client):
        tenant = TenantFactory()
        admin = _admin(tenant)
        _login(client, tenant, admin)

        # Establish a known baseline row first.
        client.patch(
            URL,
            data=json.dumps({"idle_timeout_minutes": 60, "absolute_timeout_hours": 24}),
            content_type="application/json",
        )

        resp = client.patch(
            URL,
            data=json.dumps({"idle_timeout_minutes": 90, "absolute_timeout_hours": 48}),
            content_type="application/json",
        )
        assert resp.status_code == 200, resp.content

        entry = AuditLog.all_objects.filter(tenant=tenant, action="session_settings.update").latest(
            "created_at"
        )
        assert entry.before == {"idle_timeout_minutes": 60, "absolute_timeout_hours": 24}
        assert entry.after == {"idle_timeout_minutes": 90, "absolute_timeout_hours": 48}


class TestSessionSettingsCacheInvalidation:
    # `SessionSettingsView.perform_update` defers its `cache.delete(...)` to
    # `transaction.on_commit(...)` (see that view's docstring) so a
    # concurrent request can never re-cache a stale pre-commit value. Under
    # the plain `db`/`client` fixtures (module-level `pytestmark`), each test
    # runs inside a transaction pytest-django itself never commits, so
    # Django's `on_commit` callbacks would never fire and this test would
    # falsely pass even if the cache were never invalidated at all.
    # `django_db(transaction=True)` makes this test use a REAL commit (via
    # `TransactionTestCase` semantics) so the `PATCH` request's own
    # `ATOMIC_REQUESTS` transaction actually commits and the `on_commit` hook
    # actually runs, the same as it would in production.
    @pytest.mark.django_db(transaction=True)
    def test_patch_invalidates_cache_for_subsequent_enforcement(self, client):
        tenant = TenantFactory()
        admin = _admin(tenant)
        _login(client, tenant, admin)

        # GET populates the cache via get_or_create + the middleware's own
        # per-tenant cached lookup (60s TTL).
        client.get(URL)
        cache_key = f"session_settings:{tenant.id}"

        patch_resp = client.patch(
            URL,
            data=json.dumps({"idle_timeout_minutes": 6}),
            content_type="application/json",
        )
        assert patch_resp.status_code == 200, patch_resp.content
        # perform_update explicitly deletes the cache entry.
        assert cache.get(cache_key) is None

        # Next request (well within the old 60s TTL window) must enforce the
        # NEW 6-minute idle bound, not a stale cached 60-minute default.
        now = time.time()
        session = client.session
        session[SESSION_LAST_ACTIVITY_KEY] = now - (7 * 60)
        session[SESSION_LOGIN_AT_KEY] = now - 3600
        session.save()

        resp = client.get("/api/v1/me")
        assert resp.status_code == 401, (
            "Cache was not invalidated on PATCH -- request was checked "
            "against a stale idle_timeout_minutes value."
        )


class TestLoginPathExemptFromExpiryCheck:
    def test_expired_session_hitting_login_reaches_login_view_not_timeout_401(self, client):
        tenant = TenantFactory()
        user = UserFactory(tenant=tenant)
        _login(client, tenant, user)

        # Rewind both timestamps far past both bounds -- would normally
        # `_expire()` any other endpoint.
        now = time.time()
        _set_session_key(client, SESSION_LOGIN_AT_KEY, now - (25 * 3600))
        _set_session_key(client, SESSION_LAST_ACTIVITY_KEY, now - (25 * 3600))

        # A follow-up login attempt (e.g. bad credentials) must reach
        # `LoginView` normally -- a plain 400 for bad creds, NOT the
        # `session-expired` 401 `SessionTimeoutMiddleware` would otherwise
        # produce on every other endpoint in this state.
        resp = client.post(
            "/api/v1/auth/login",
            {"tenant": tenant.slug, "email": user.email, "password": "wrong-password"},
            content_type="application/json",
        )
        assert resp.status_code in (400, 401), resp.content
        if resp.status_code == 401:
            assert "session-expired" not in resp.json()["type"]

        # A GOOD login attempt on the same stale cookie must succeed (200),
        # not be intercepted as an expired session either.
        good_resp = _login(client, tenant, user)
        assert good_resp.status_code == 200


class TestLoginStampsTimeouts:
    def test_login_stamps_both_timestamps(self, client):
        tenant = TenantFactory()
        user = UserFactory(tenant=tenant)

        before = time.time()
        _login(client, tenant, user)
        after = time.time()

        session = client.session
        assert TENANT_SESSION_KEY in session
        assert before - 5 <= session[SESSION_LOGIN_AT_KEY] <= after + 5
        assert before - 5 <= session[SESSION_LAST_ACTIVITY_KEY] <= after + 5
