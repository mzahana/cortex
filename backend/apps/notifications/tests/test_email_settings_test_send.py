"""Acceptance tests for `POST /api/v1/notifications/email-settings/test`
(`EmailSettingsTestView`) -- lets a tenant admin verify their just-saved
Brevo config (API key + sender) actually works, without waiting for a real
domain event. Uses `EmailProvider.send_test_email(...)`, which needs no
Brevo template/template id at all (see `providers.py`/`test_brevo_provider.
py`'s `TestBrevoSendTestEmail`) -- only a real API key and sender.

Covers the security-sensitive properties this endpoint's posture depends on:
- RBAC: `tenant.manage` gates this the same way it gates
  `GET/PUT /api/v1/notifications/email-settings` (non-admin gets 403, no send
  is attempted, no `EmailLog` row is written);
- anti-abuse: the recipient is ALWAYS `request.user.email`, never any
  client-supplied address, even if the request body tries to smuggle one in;
- success path: writes a `sent` `EmailLog` row and returns the documented
  200 body;
- failure path (`EmailSendError`, e.g. no sender configured, or no Brevo API
  key configured): writes a `failed` `EmailLog` row and returns a 400
  RFC-7807 problem+json body, not a 500;
- tenant isolation (R4): one tenant's test-send never reads/writes another
  tenant's `EmailSettings`/`EmailLog` rows.

Same real-HTTP-request-via-`client` convention as
`test_email_settings_api.py`.
"""

from __future__ import annotations

import json

import pytest

from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
    TenantFactory,
    UserFactory,
    upgrade_tenant_wide_role,
)
from apps.notifications.crypto import encrypt_api_key
from apps.notifications.models import EmailLog, EmailSettings
from apps.rbac.permission_keys import ROLE_ADMIN
from apps.tenancy.context import tenant_context

pytestmark = pytest.mark.django_db

URL = "/api/v1/notifications/email-settings/test"


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


class TestEmailSettingsTestSendRBAC:
    def test_member_gets_403_and_no_send_is_attempted(self, client):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)  # default tenant-wide Member
        _login(client, tenant, member)

        resp = client.post(URL, data=json.dumps({}), content_type="application/json")
        assert resp.status_code == 403, resp.content

        with tenant_context(tenant.id):
            assert EmailLog.objects.filter(event_type="test_email").count() == 0

    def test_anonymous_gets_401_or_403(self, client):
        resp = client.post(URL, data=json.dumps({}), content_type="application/json")
        assert resp.status_code in (401, 403)


class TestEmailSettingsTestSendSuccess:
    def test_console_provider_default_succeeds_and_writes_sent_log(self, client):
        """No `EmailSettings` row configured at all -> `get_email_provider()`
        falls back to the env default, which is `ConsoleProvider` in every
        test/dev environment (`providers.py` module docstring) -- it never
        fails, so this is the plain success path with zero extra setup."""
        tenant = TenantFactory()
        admin = _admin(tenant)
        _login(client, tenant, admin)

        resp = client.post(URL, data=json.dumps({}), content_type="application/json")
        assert resp.status_code == 200, resp.content
        body = resp.json()
        assert body["status"] == "sent"
        assert body["provider"] == "ConsoleProvider"
        assert body["sent_to"] == admin.email

        with tenant_context(tenant.id):
            log = EmailLog.objects.filter(event_type="test_email").latest("created_at")
        assert log.status == EmailLog.Status.SENT
        assert log.recipient == admin.email
        assert log.user_id == admin.id
        assert log.provider == "ConsoleProvider"
        assert log.provider_message_id  # console provider always sets one

    def test_recipient_is_always_the_caller_never_request_body(self, client):
        """Anti-abuse: even if the body tries to smuggle an arbitrary `to`,
        the send (and the resulting `EmailLog` row) always target the
        authenticated caller's own email."""
        tenant = TenantFactory()
        admin = _admin(tenant)
        _login(client, tenant, admin)

        resp = client.post(
            URL,
            data=json.dumps({"to": "attacker@evil.test"}),
            content_type="application/json",
        )
        assert resp.status_code == 200, resp.content
        body = resp.json()
        assert body["sent_to"] == admin.email
        assert body["sent_to"] != "attacker@evil.test"

        with tenant_context(tenant.id):
            log = EmailLog.objects.filter(event_type="test_email").latest("created_at")
        assert log.recipient == admin.email

    def test_brevo_with_only_api_key_and_sender_succeeds_no_template_needed(
        self, client, monkeypatch
    ):
        """End-to-end proof of the actual UX fix: a tenant with Brevo API
        key + sender saved -- and no template configuration of any kind --
        can still send a successful test email, because `send_test_email`
        builds its content locally and never needs a Brevo templateId."""
        import json as _json
        import urllib.request

        tenant = TenantFactory()
        admin = _admin(tenant)

        with tenant_context(tenant.id):
            EmailSettings.objects.create(
                tenant=tenant,
                provider=EmailSettings.Provider.BREVO,
                sender_email="notifications@example.test",
                api_key_encrypted=encrypt_api_key("sk-fake-key"),
                api_key_last4="fake",
            )

        class _FakeResponse:
            def read(self):
                return _json.dumps({"messageId": "fake-id"}).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        captured = {}

        def fake_urlopen(request, timeout=10):
            captured["payload"] = _json.loads(request.data.decode("utf-8"))
            return _FakeResponse()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        _login(client, tenant, admin)
        resp = client.post(URL, data=json.dumps({}), content_type="application/json")
        assert resp.status_code == 200, resp.content
        assert resp.json()["provider"] == "BrevoProvider"
        assert "templateId" not in captured["payload"]

        with tenant_context(tenant.id):
            log = EmailLog.objects.filter(event_type="test_email").latest("created_at")
        assert log.status == EmailLog.Status.SENT


class TestEmailSettingsTestSendFailure:
    def test_brevo_selected_without_api_key_returns_400_and_writes_failed_log(
        self, client, settings
    ):
        tenant = TenantFactory()
        admin = _admin(tenant)
        _login(client, tenant, admin)

        # `.env`'s `BREVO_API_KEY` placeholder leaks into every env-driven
        # setting by default -- override it to empty so this test genuinely
        # exercises the "no key configured at all" branch of
        # `BrevoProvider.send_transactional`, independent of local/CI env.
        settings.BREVO_API_KEY = ""

        with tenant_context(tenant.id):
            EmailSettings.objects.create(tenant=tenant, provider=EmailSettings.Provider.BREVO)

        resp = client.post(URL, data=json.dumps({}), content_type="application/json")
        assert resp.status_code == 400, resp.content
        problem = resp.json()
        assert problem["status"] == 400
        assert "BREVO_API_KEY" in problem["detail"] or "api" in problem["detail"].lower()

        with tenant_context(tenant.id):
            log = EmailLog.objects.filter(event_type="test_email").latest("created_at")
        assert log.status == EmailLog.Status.FAILED
        assert log.error
        assert log.recipient == admin.email

    def test_rotated_encryption_key_fails_closed_not_500(self, client, settings):
        """`get_email_provider()` resolving a tenant's stored API key
        DECRYPTS it (`crypto.decrypt_api_key`) -- if `EMAIL_SETTINGS_
        ENCRYPTION_KEY` was ever rotated/regenerated after the key was
        stored, that raises `cryptography.fernet.InvalidToken`, not
        `EmailSendError`. This must still come back as a clean 400 with a
        `failed` `EmailLog` row (the endpoint's whole audit-trail
        contract), never an unhandled 500 with no log at all."""
        from cryptography.fernet import Fernet

        tenant = TenantFactory()
        admin = _admin(tenant)

        settings.EMAIL_SETTINGS_ENCRYPTION_KEY = Fernet.generate_key().decode()
        with tenant_context(tenant.id):
            EmailSettings.objects.create(
                tenant=tenant,
                provider=EmailSettings.Provider.BREVO,
                sender_email="notifications@example.test",
                api_key_encrypted=encrypt_api_key("sk-fake-key"),
                api_key_last4="fake",
            )
        # Rotate the key AFTER encrypting -- simulates an operator
        # regenerating `EMAIL_SETTINGS_ENCRYPTION_KEY` without re-saving
        # every tenant's stored API key.
        settings.EMAIL_SETTINGS_ENCRYPTION_KEY = Fernet.generate_key().decode()

        _login(client, tenant, admin)
        resp = client.post(URL, data=json.dumps({}), content_type="application/json")
        assert resp.status_code == 400, resp.content
        problem = resp.json()
        assert problem["status"] == 400

        with tenant_context(tenant.id):
            log = EmailLog.objects.filter(event_type="test_email").latest("created_at")
        assert log.status == EmailLog.Status.FAILED
        assert log.error

    def test_missing_sender_returns_400_and_writes_failed_log(self, client):
        """`send_test_email` needs no Brevo template mapping at all (unlike
        `send_transactional`) -- but it DOES still need a sender address,
        with no implicit fallback, so a saved API key alone isn't enough."""
        tenant = TenantFactory()
        admin = _admin(tenant)
        _login(client, tenant, admin)

        with tenant_context(tenant.id):
            EmailSettings.objects.create(
                tenant=tenant,
                provider=EmailSettings.Provider.BREVO,
                api_key_encrypted=encrypt_api_key("sk-fake-key"),
                api_key_last4="fake",
                sender_email="",
            )

        resp = client.post(URL, data=json.dumps({}), content_type="application/json")
        assert resp.status_code == 400, resp.content
        problem = resp.json()
        assert "sender" in problem["detail"].lower()

        with tenant_context(tenant.id):
            log = EmailLog.objects.filter(event_type="test_email").latest("created_at")
        assert log.status == EmailLog.Status.FAILED
        assert "sender" in log.error.lower()


class TestEmailSettingsTestSendTenantIsolation:
    def test_tenant_a_test_send_never_touches_tenant_b_rows(self, client):
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        admin_a = _admin(tenant_a)

        with tenant_context(tenant_b.id):
            EmailSettings.objects.create(
                tenant=tenant_b,
                provider=EmailSettings.Provider.BREVO,
                sender_email="notifications@example.test",
                api_key_encrypted=encrypt_api_key("tenant-b-secret"),
                api_key_last4="cret",
            )

        _login(client, tenant_a, admin_a)
        resp = client.post(URL, data=json.dumps({}), content_type="application/json")
        # Tenant A has no EmailSettings row -> falls back to the env default
        # (ConsoleProvider), completely independent of tenant B's Brevo
        # config -- proves tenant B's row was never read.
        assert resp.status_code == 200, resp.content
        assert resp.json()["provider"] == "ConsoleProvider"

        with tenant_context(tenant_a.id):
            a_logs = EmailLog.objects.filter(event_type="test_email")
        assert a_logs.count() == 1

        with tenant_context(tenant_b.id):
            b_logs = EmailLog.objects.filter(event_type="test_email")
        assert b_logs.count() == 0
