"""Acceptance tests for the password-management feature set (branch
`feature/project-grants`): self-service change (`POST /api/v1/me/password`),
admin reset (`POST /api/v1/users/{id}/reset-password/`), and the forgot-
password flow (`POST /api/v1/auth/password-reset/request` +
`POST /api/v1/auth/password-reset/confirm`).

Covers:
- `TestChangePassword` -- happy path, wrong current password, weak new
  password, unauthenticated, audit hygiene, query budget.
- `TestAdminResetPassword` -- happy path, cross-tenant 404, RBAC (Member/
  ProjectLead denied), audit hygiene.
- `TestForgotPasswordRequest` -- generic response regardless of hit/miss,
  token minted only on a genuine hit, only the newest token stays live,
  inactive users get no token, only the hash is ever persisted.
- `TestForgotPasswordConfirm` -- happy path, single-use, expiry, wrong-
  tenant, weak password leaves the token live, audit hygiene, query budget.
- `TestPasswordResetTokenTenantIsolation` -- RLS-level visibility
  differential on `accounts_password_reset_token` (mirrors
  `apps.common.tests.test_rls_canonical`).
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.models import PasswordResetToken
from apps.accounts.services import _hash_reset_token
from apps.audit.models import AuditLog
from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
    ProjectFactory,
    TenantFactory,
    UserFactory,
    add_project_membership,
    upgrade_tenant_wide_role,
)
from apps.notifications.models import EmailLog
from apps.rbac.permission_keys import ROLE_ADMIN, ROLE_PROJECT_LEAD
from apps.tenancy.context import tenant_context
from conftest import set_app_role_tenant

pytestmark = pytest.mark.django_db

NEW_STRONG_PASSWORD = "Str0ng-Br4nd-New-Pw!"


def _login(client, tenant, user, password=DEFAULT_TEST_PASSWORD):
    response = client.post(
        "/api/v1/auth/login",
        {"tenant": tenant.slug, "email": user.email, "password": password},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return response


class TestChangePassword:
    def test_happy_path_updates_password_and_keeps_session(self, client):
        tenant = TenantFactory()
        user = UserFactory(tenant=tenant)
        _login(client, tenant, user)

        response = client.post(
            "/api/v1/me/password",
            data=json.dumps(
                {
                    "current_password": DEFAULT_TEST_PASSWORD,
                    "new_password": NEW_STRONG_PASSWORD,
                }
            ),
            content_type="application/json",
        )
        assert response.status_code == 204, response.content

        # Session survived the password change (update_session_auth_hash).
        me = client.get("/api/v1/me")
        assert me.status_code == 200, me.content

        user.refresh_from_db()
        assert user.check_password(NEW_STRONG_PASSWORD)
        assert not user.check_password(DEFAULT_TEST_PASSWORD)

        # Old password no longer logs in on a fresh client; new one does.
        from django.test import Client

        fresh = Client()
        old_login = fresh.post(
            "/api/v1/auth/login",
            {"tenant": tenant.slug, "email": user.email, "password": DEFAULT_TEST_PASSWORD},
            content_type="application/json",
        )
        assert old_login.status_code == 401

        new_login = fresh.post(
            "/api/v1/auth/login",
            {"tenant": tenant.slug, "email": user.email, "password": NEW_STRONG_PASSWORD},
            content_type="application/json",
        )
        assert new_login.status_code == 200, new_login.content

    def test_wrong_current_password_is_rejected(self, client):
        tenant = TenantFactory()
        user = UserFactory(tenant=tenant)
        _login(client, tenant, user)

        response = client.post(
            "/api/v1/me/password",
            data=json.dumps(
                {"current_password": "totally-wrong", "new_password": NEW_STRONG_PASSWORD}
            ),
            content_type="application/json",
        )
        assert response.status_code == 400, response.content
        assert "current_password" in response.json()["errors"]

        user.refresh_from_db()
        assert user.check_password(DEFAULT_TEST_PASSWORD)

    def test_weak_new_password_is_rejected(self, client):
        tenant = TenantFactory()
        user = UserFactory(tenant=tenant)
        _login(client, tenant, user)

        response = client.post(
            "/api/v1/me/password",
            data=json.dumps({"current_password": DEFAULT_TEST_PASSWORD, "new_password": "abc"}),
            content_type="application/json",
        )
        assert response.status_code == 400, response.content
        assert "new_password" in response.json()["errors"]

        user.refresh_from_db()
        assert user.check_password(DEFAULT_TEST_PASSWORD)

    def test_unauthenticated_is_rejected(self, client):
        response = client.post(
            "/api/v1/me/password",
            data=json.dumps({"current_password": "whatever", "new_password": NEW_STRONG_PASSWORD}),
            content_type="application/json",
        )
        assert response.status_code in (401, 403)

    def test_audit_entry_written_without_password_material(self, client):
        tenant = TenantFactory()
        user = UserFactory(tenant=tenant)
        _login(client, tenant, user)

        response = client.post(
            "/api/v1/me/password",
            data=json.dumps(
                {
                    "current_password": DEFAULT_TEST_PASSWORD,
                    "new_password": NEW_STRONG_PASSWORD,
                }
            ),
            content_type="application/json",
        )
        assert response.status_code == 204, response.content

        entries = AuditLog.all_objects.filter(
            tenant_id=tenant.id, entity_type="user", entity_id=str(user.id)
        )
        assert entries.count() == 1
        entry = entries.first()
        assert entry.action == "user.password_change"
        assert entry.actor_id == user.id
        assert entry.before is None
        assert entry.after == {"id": user.id, "email": user.email}

        serialized = json.dumps({"before": entry.before, "after": entry.after})
        assert DEFAULT_TEST_PASSWORD not in serialized
        assert NEW_STRONG_PASSWORD not in serialized
        assert "password" not in serialized.lower()

    def test_query_budget(self, client, django_assert_max_num_queries):
        tenant = TenantFactory()
        user = UserFactory(tenant=tenant)
        _login(client, tenant, user)

        with django_assert_max_num_queries(15):
            response = client.post(
                "/api/v1/me/password",
                data=json.dumps(
                    {
                        "current_password": DEFAULT_TEST_PASSWORD,
                        "new_password": NEW_STRONG_PASSWORD,
                    }
                ),
                content_type="application/json",
            )
        assert response.status_code == 204, response.content


class TestAdminResetPassword:
    def test_admin_resets_user_in_tenant(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        target = UserFactory(tenant=tenant, email="target@example.test")

        _login(client, tenant, admin)
        response = client.post(f"/api/v1/users/{target.id}/reset-password/")
        assert response.status_code == 200, response.content
        body = response.json()
        assert body["id"] == target.id
        assert body["email"] == target.email
        new_password = body["password"]
        assert new_password

        target.refresh_from_db()
        assert target.check_password(new_password)
        assert not target.check_password(DEFAULT_TEST_PASSWORD)

        from django.test import Client

        fresh = Client()
        login_response = fresh.post(
            "/api/v1/auth/login",
            {"tenant": tenant.slug, "email": target.email, "password": new_password},
            content_type="application/json",
        )
        assert login_response.status_code == 200, login_response.content

        old_login = fresh.post(
            "/api/v1/auth/login",
            {"tenant": tenant.slug, "email": target.email, "password": DEFAULT_TEST_PASSWORD},
            content_type="application/json",
        )
        assert old_login.status_code == 401

    def test_audit_entry_written_without_password_material(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        target = UserFactory(tenant=tenant, email="audited-target@example.test")

        _login(client, tenant, admin)
        response = client.post(f"/api/v1/users/{target.id}/reset-password/")
        assert response.status_code == 200, response.content
        new_password = response.json()["password"]

        entries = AuditLog.all_objects.filter(
            tenant_id=tenant.id, entity_type="user", entity_id=str(target.id)
        )
        assert entries.count() == 1
        entry = entries.first()
        assert entry.action == "user.password_reset"
        assert entry.actor_id == admin.id
        assert entry.before is None
        assert entry.after == {"id": target.id, "email": target.email, "name": target.name}
        assert "password" not in entry.after

        serialized = json.dumps(entry.after)
        assert new_password not in serialized

    def test_cross_tenant_target_is_404(self, client):
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        admin_a = UserFactory(tenant=tenant_a)
        upgrade_tenant_wide_role(admin_a, ROLE_ADMIN)
        target_b = UserFactory(tenant=tenant_b, email="cross-tenant-target@example.test")

        _login(client, tenant_a, admin_a)
        response = client.post(f"/api/v1/users/{target_b.id}/reset-password/")
        assert response.status_code == 404, response.content

        target_b.refresh_from_db()
        assert target_b.check_password(DEFAULT_TEST_PASSWORD)

    def test_member_without_user_manage_is_403(self, client):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        target = UserFactory(tenant=tenant, email="member-cannot-reset@example.test")

        _login(client, tenant, member)
        response = client.post(f"/api/v1/users/{target.id}/reset-password/")
        assert response.status_code == 403

        target.refresh_from_db()
        assert target.check_password(DEFAULT_TEST_PASSWORD)

    def test_project_scoped_lead_without_tenant_wide_user_manage_is_403(self, client):
        """`reset_password` is in `_ADMIN_ONLY_USER_ACTIONS` -- a project-scoped
        `user.manage` grant (ProjectLead) must NOT suffice, mirroring `create`'s
        Admin-only gate (see `apps.accounts.permissions` docstring)."""
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, project, ROLE_PROJECT_LEAD)
        target = UserFactory(tenant=tenant, email="lead-cannot-reset@example.test")

        _login(client, tenant, lead)
        response = client.post(f"/api/v1/users/{target.id}/reset-password/")
        assert response.status_code == 403

        target.refresh_from_db()
        assert target.check_password(DEFAULT_TEST_PASSWORD)

    def test_guessed_url_for_cross_tenant_id_from_member_is_still_blocked(self, client):
        """Even a guessed URL against another tenant's user id must not leak
        existence via a different status code for a non-admin caller."""
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        member_a = UserFactory(tenant=tenant_a)
        target_b = UserFactory(tenant=tenant_b, email="guessed@example.test")

        _login(client, tenant_a, member_a)
        response = client.post(f"/api/v1/users/{target_b.id}/reset-password/")
        assert response.status_code == 403


class TestForgotPasswordRequest:
    def test_existing_active_user_gets_generic_200_and_mints_one_token_and_email(
        self, client, django_capture_on_commit_callbacks, monkeypatch
    ):
        import apps.accounts.api as accounts_api

        tenant = TenantFactory()
        user = UserFactory(tenant=tenant, email="forgetful@example.test")

        # Capture the params handed to `enqueue_transactional_email` (the
        # emailed reset link, specifically) so we can recover the RAW token
        # -- it is never persisted anywhere, only mailed -- and prove the DB
        # only ever stores its SHA-256 hash.
        captured: dict = {}
        real_enqueue = accounts_api.enqueue_transactional_email

        def _spy_enqueue(**kwargs):
            captured.update(kwargs)
            return real_enqueue(**kwargs)

        monkeypatch.setattr(accounts_api, "enqueue_transactional_email", _spy_enqueue)

        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(
                "/api/v1/auth/password-reset/request",
                data=json.dumps({"tenant": tenant.slug, "email": user.email}),
                content_type="application/json",
            )
        assert response.status_code == 200, response.content
        generic_body = response.json()

        with tenant_context(tenant.id):
            tokens = list(PasswordResetToken.objects.filter(user=user))
        assert len(tokens) == 1
        token = tokens[0]
        assert token.used_at is None
        assert len(token.token_hash) == 64

        from urllib.parse import parse_qs, urlparse

        reset_url = captured["params"]["reset_url"]
        raw_token = parse_qs(urlparse(reset_url).query)["token"][0]
        # Only the SHA-256 hash is ever persisted -- never the raw/guessable
        # value that was actually emailed.
        assert token.token_hash != raw_token
        assert token.token_hash == hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

        logs = EmailLog.all_objects.filter(tenant_id=tenant.id, event_type="password_reset")
        assert logs.count() == 1
        log = logs.get()
        assert log.recipient == user.email
        assert log.status == EmailLog.Status.SENT

        assert response.json() == generic_body

    def test_nonexistent_email_same_tenant_gets_same_generic_response_no_token(
        self, client, django_capture_on_commit_callbacks
    ):
        tenant = TenantFactory()

        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(
                "/api/v1/auth/password-reset/request",
                data=json.dumps({"tenant": tenant.slug, "email": "nobody@example.test"}),
                content_type="application/json",
            )
        assert response.status_code == 200, response.content

        with tenant_context(tenant.id):
            assert not PasswordResetToken.objects.exists()
        assert not EmailLog.all_objects.filter(tenant_id=tenant.id).exists()

    def test_nonexistent_tenant_gets_same_generic_response(
        self, client, django_capture_on_commit_callbacks
    ):
        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(
                "/api/v1/auth/password-reset/request",
                data=json.dumps({"tenant": "no-such-tenant-slug", "email": "x@example.test"}),
                content_type="application/json",
            )
        assert response.status_code == 200, response.content
        assert "password-reset link has been sent" in response.json()["detail"]

    def test_response_body_identical_for_hit_and_miss(
        self, client, django_capture_on_commit_callbacks
    ):
        tenant = TenantFactory()
        user = UserFactory(tenant=tenant, email="real@example.test")

        with django_capture_on_commit_callbacks(execute=True):
            hit = client.post(
                "/api/v1/auth/password-reset/request",
                data=json.dumps({"tenant": tenant.slug, "email": user.email}),
                content_type="application/json",
            )
            miss = client.post(
                "/api/v1/auth/password-reset/request",
                data=json.dumps({"tenant": tenant.slug, "email": "no-such-user@example.test"}),
                content_type="application/json",
            )
        assert hit.status_code == miss.status_code == 200
        assert hit.json() == miss.json()

    def test_requesting_twice_invalidates_the_first_token(
        self, client, django_capture_on_commit_callbacks
    ):
        tenant = TenantFactory()
        user = UserFactory(tenant=tenant, email="double-request@example.test")

        with django_capture_on_commit_callbacks(execute=True):
            client.post(
                "/api/v1/auth/password-reset/request",
                data=json.dumps({"tenant": tenant.slug, "email": user.email}),
                content_type="application/json",
            )
            client.post(
                "/api/v1/auth/password-reset/request",
                data=json.dumps({"tenant": tenant.slug, "email": user.email}),
                content_type="application/json",
            )

        with tenant_context(tenant.id):
            tokens = list(PasswordResetToken.objects.filter(user=user).order_by("id"))
        assert len(tokens) == 2
        assert tokens[0].used_at is not None  # first invalidated
        assert tokens[1].used_at is None  # only the newest is live

    def test_inactive_user_gets_generic_200_and_no_token(
        self, client, django_capture_on_commit_callbacks
    ):
        tenant = TenantFactory()
        user = UserFactory(tenant=tenant, email="inactive@example.test", is_active=False)

        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(
                "/api/v1/auth/password-reset/request",
                data=json.dumps({"tenant": tenant.slug, "email": user.email}),
                content_type="application/json",
            )
        assert response.status_code == 200, response.content

        with tenant_context(tenant.id):
            assert not PasswordResetToken.objects.filter(user=user).exists()
        assert not EmailLog.all_objects.filter(tenant_id=tenant.id).exists()


def _mint_token(tenant, user) -> tuple[str, PasswordResetToken]:
    from apps.accounts.services import create_password_reset_token

    with tenant_context(tenant.id):
        raw_token = create_password_reset_token(user)
        token = PasswordResetToken.objects.get(token_hash=_hash_reset_token(raw_token))
    return raw_token, token


class TestForgotPasswordConfirm:
    def test_happy_path_changes_password_and_consumes_token(self, client):
        tenant = TenantFactory()
        user = UserFactory(tenant=tenant, email="confirm-happy@example.test")
        raw_token, token = _mint_token(tenant, user)

        response = client.post(
            "/api/v1/auth/password-reset/confirm",
            data=json.dumps(
                {"tenant": tenant.slug, "token": raw_token, "new_password": NEW_STRONG_PASSWORD}
            ),
            content_type="application/json",
        )
        assert response.status_code == 204, response.content

        user.refresh_from_db()
        assert user.check_password(NEW_STRONG_PASSWORD)
        assert not user.check_password(DEFAULT_TEST_PASSWORD)

        with tenant_context(tenant.id):
            token.refresh_from_db()
        assert token.used_at is not None

    def test_reusing_the_same_token_is_rejected(self, client):
        tenant = TenantFactory()
        user = UserFactory(tenant=tenant, email="confirm-reuse@example.test")
        raw_token, _token = _mint_token(tenant, user)

        first = client.post(
            "/api/v1/auth/password-reset/confirm",
            data=json.dumps(
                {"tenant": tenant.slug, "token": raw_token, "new_password": NEW_STRONG_PASSWORD}
            ),
            content_type="application/json",
        )
        assert first.status_code == 204, first.content

        second = client.post(
            "/api/v1/auth/password-reset/confirm",
            data=json.dumps(
                {
                    "tenant": tenant.slug,
                    "token": raw_token,
                    "new_password": "Another-Str0ng-Pw!",
                }
            ),
            content_type="application/json",
        )
        assert second.status_code == 400, second.content
        assert "invalid-reset-token" in second.json()["type"]

    def test_expired_token_is_rejected(self, client):
        tenant = TenantFactory()
        user = UserFactory(tenant=tenant, email="confirm-expired@example.test")
        raw_token, token = _mint_token(tenant, user)

        with tenant_context(tenant.id):
            PasswordResetToken.objects.filter(pk=token.pk).update(
                expires_at=timezone.now() - timedelta(seconds=1)
            )

        response = client.post(
            "/api/v1/auth/password-reset/confirm",
            data=json.dumps(
                {"tenant": tenant.slug, "token": raw_token, "new_password": NEW_STRONG_PASSWORD}
            ),
            content_type="application/json",
        )
        assert response.status_code == 400, response.content
        assert "invalid-reset-token" in response.json()["type"]

        user.refresh_from_db()
        assert user.check_password(DEFAULT_TEST_PASSWORD)

    def test_wrong_tenant_slug_for_a_real_token_is_rejected_no_cross_tenant_consumption(
        self, client
    ):
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        user_a = UserFactory(tenant=tenant_a, email="cross-tenant-confirm@example.test")
        raw_token, token = _mint_token(tenant_a, user_a)

        response = client.post(
            "/api/v1/auth/password-reset/confirm",
            data=json.dumps(
                {"tenant": tenant_b.slug, "token": raw_token, "new_password": NEW_STRONG_PASSWORD}
            ),
            content_type="application/json",
        )
        assert response.status_code == 400, response.content
        assert "invalid-reset-token" in response.json()["type"]

        # Token still live/unconsumed -- the correct tenant can still use it.
        with tenant_context(tenant_a.id):
            token.refresh_from_db()
        assert token.used_at is None

        ok = client.post(
            "/api/v1/auth/password-reset/confirm",
            data=json.dumps(
                {"tenant": tenant_a.slug, "token": raw_token, "new_password": NEW_STRONG_PASSWORD}
            ),
            content_type="application/json",
        )
        assert ok.status_code == 204, ok.content

    def test_weak_new_password_leaves_token_live_for_retry(self, client):
        tenant = TenantFactory()
        user = UserFactory(tenant=tenant, email="confirm-weak@example.test")
        raw_token, token = _mint_token(tenant, user)

        weak = client.post(
            "/api/v1/auth/password-reset/confirm",
            data=json.dumps({"tenant": tenant.slug, "token": raw_token, "new_password": "abc"}),
            content_type="application/json",
        )
        assert weak.status_code == 400, weak.content
        assert "new_password" in weak.json()["errors"]

        with tenant_context(tenant.id):
            token.refresh_from_db()
        assert token.used_at is None

        # Retry with a strong password on the SAME token succeeds.
        retry = client.post(
            "/api/v1/auth/password-reset/confirm",
            data=json.dumps(
                {"tenant": tenant.slug, "token": raw_token, "new_password": NEW_STRONG_PASSWORD}
            ),
            content_type="application/json",
        )
        assert retry.status_code == 204, retry.content

    def test_audit_entry_written(self, client):
        tenant = TenantFactory()
        user = UserFactory(tenant=tenant, email="confirm-audit@example.test")
        raw_token, _token = _mint_token(tenant, user)

        response = client.post(
            "/api/v1/auth/password-reset/confirm",
            data=json.dumps(
                {"tenant": tenant.slug, "token": raw_token, "new_password": NEW_STRONG_PASSWORD}
            ),
            content_type="application/json",
        )
        assert response.status_code == 204, response.content

        entries = AuditLog.all_objects.filter(
            tenant_id=tenant.id, entity_type="user", entity_id=str(user.id)
        )
        confirm_entries = entries.filter(action="user.password_reset_confirm")
        assert confirm_entries.count() == 1
        entry = confirm_entries.first()
        assert entry.actor_id is None
        assert entry.after == {"id": user.id, "email": user.email}
        serialized = json.dumps(entry.after)
        assert NEW_STRONG_PASSWORD not in serialized

    def test_query_budget(self, client, django_assert_max_num_queries):
        tenant = TenantFactory()
        user = UserFactory(tenant=tenant, email="confirm-budget@example.test")
        raw_token, _token = _mint_token(tenant, user)

        with django_assert_max_num_queries(15):
            response = client.post(
                "/api/v1/auth/password-reset/confirm",
                data=json.dumps(
                    {
                        "tenant": tenant.slug,
                        "token": raw_token,
                        "new_password": NEW_STRONG_PASSWORD,
                    }
                ),
                content_type="application/json",
            )
        assert response.status_code == 204, response.content


@pytest.mark.django_db(transaction=True)
def test_password_reset_token_rls_visibility_differential(app_role_connection):
    """Mirrors `apps.common.tests.test_rls_canonical`'s canonical template: a
    SAME committed `PasswordResetToken` row, visible or invisible SOLELY
    based on which tenant `app.current_tenant` is set to on the real
    `cortex_app` role connection -- the R4 backstop for a missing/forgotten
    app-level tenant filter on this brand-new table."""
    tenant_a = TenantFactory()
    tenant_b = TenantFactory()
    user_a = UserFactory(tenant=tenant_a)

    token = PasswordResetToken.all_objects.create(
        tenant=tenant_a,
        user=user_a,
        token_hash="a" * 64,
        expires_at=timezone.now() + timedelta(hours=1),
    )

    with app_role_connection.cursor() as cur:
        # Positive: GUC set to the row's OWN tenant -> visible.
        set_app_role_tenant(app_role_connection, tenant_a.id)
        cur.execute("SELECT id FROM accounts_password_reset_token WHERE id = %s", [token.id])
        assert cur.fetchone() is not None, (
            "RLS hid a PasswordResetToken row from its OWN tenant's GUC -- "
            "the policy predicate is broken, not just strict."
        )

        # Negative control #1: GUC flipped to a DIFFERENT tenant -> the SAME
        # committed row must become invisible.
        set_app_role_tenant(app_role_connection, tenant_b.id)
        cur.execute("SELECT id FROM accounts_password_reset_token WHERE id = %s", [token.id])
        assert cur.fetchone() is None, (
            "RLS did NOT block a cross-tenant SELECT on "
            "accounts_password_reset_token -- tenant B's GUC could see "
            "tenant A's reset token."
        )

        # Negative control #2: GUC cleared entirely -> fail-closed.
        set_app_role_tenant(app_role_connection, None)
        cur.execute("SELECT id FROM accounts_password_reset_token WHERE id = %s", [token.id])
        assert cur.fetchone() is None, "RLS did NOT fail closed with no tenant in context."

        # Back to the owning tenant -> visible again.
        set_app_role_tenant(app_role_connection, tenant_a.id)
        cur.execute("SELECT id FROM accounts_password_reset_token WHERE id = %s", [token.id])
        assert cur.fetchone() is not None
