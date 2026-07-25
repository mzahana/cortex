"""Acceptance tests for `GET/PUT/PATCH /api/v1/notifications/email-settings`
(per-tenant email delivery config: provider, sender, Brevo API key).

Covers the security-sensitive properties this feature's posture depends on:
- tenant isolation (R4): tenant B's session can never see/touch tenant A's row;
- RBAC: `tenant.manage` gates BOTH read and write (the `view_key` override in
  `EmailSettingsView.permission_classes` is the review-relevant thing to prove);
- secret handling: the raw API key never round-trips in a response body and
  never appears in the resulting `AuditLog` before/after JSON;
- omit/clear/set write semantics for the write-only `api_key` field;
- `get_or_create` singleton semantics (exactly one row per tenant, no race).

Real HTTP requests via `client` (pytest-django's `Client`), through the full
middleware stack, matching `apps.catalog.tests.test_catalog_api`'s convention
for this exact "tenant-wide, admin-gated singleton resource" shape.
"""

from __future__ import annotations

import json

import pytest

from apps.audit.models import AuditLog
from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
    TenantFactory,
    UserFactory,
    upgrade_tenant_wide_role,
)
from apps.notifications.crypto import encrypt_api_key
from apps.notifications.models import EmailSettings
from apps.rbac.permission_keys import ROLE_ADMIN
from apps.tenancy.context import tenant_context

pytestmark = pytest.mark.django_db

URL = "/api/v1/notifications/email-settings"


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


class TestEmailSettingsTenantIsolation:
    def test_tenant_b_never_sees_or_edits_tenant_as_row(self, client):
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        admin_a = _admin(tenant_a)
        admin_b = _admin(tenant_b)

        # Tenant A already has a configured row, including a real secret.
        row_a = EmailSettings.all_objects.create(
            tenant=tenant_a,
            provider=EmailSettings.Provider.BREVO,
            sender_email="a-sender@tenant-a.test",
            reply_to="a-reply@tenant-a.test",
            api_key_encrypted=encrypt_api_key("tenant-a-secret-key"),
            api_key_last4="-key",
        )

        _login(client, tenant_b, admin_b)

        # GET as tenant B must never return tenant A's data -- it transparently
        # get_or_creates tenant B's OWN (separate) default row instead.
        get_resp = client.get(URL)
        assert get_resp.status_code == 200, get_resp.content
        body = get_resp.json()
        assert body["sender_email"] != row_a.sender_email
        assert body["reply_to"] != row_a.reply_to
        assert body["has_api_key"] is False
        assert "tenant-a-secret-key" not in get_resp.content.decode()

        # PUT as tenant B must never touch tenant A's row.
        put_resp = client.put(
            URL,
            data=json.dumps(
                {
                    "provider": "console",
                    "sender_email": "b-sender@tenant-b.test",
                    "reply_to": "b-reply@tenant-b.test",
                }
            ),
            content_type="application/json",
        )
        assert put_resp.status_code == 200, put_resp.content

        # Confirm directly in the DB (owner-role connection, tenant_context
        # scoped explicitly) that tenant A's row is byte-for-byte unchanged.
        with tenant_context(tenant_a.id):
            row_a.refresh_from_db()
        assert row_a.sender_email == "a-sender@tenant-a.test"
        assert row_a.reply_to == "a-reply@tenant-a.test"
        assert row_a.api_key_last4 == "-key"
        # Exactly one row per tenant -- tenant B's PUT created/updated its OWN
        # row, never a second row for tenant A and never mutated tenant A's.
        assert EmailSettings.all_objects.filter(tenant=tenant_a).count() == 1
        assert EmailSettings.all_objects.filter(tenant=tenant_b).count() == 1
        assert EmailSettings.all_objects.get(tenant=tenant_b).sender_email == (
            "b-sender@tenant-b.test"
        )

    def test_guessed_id_style_access_is_impossible_singleton_is_self_scoped(self, client):
        """There is no `{id}` in this endpoint's URL to guess -- `get_object()`
        always resolves via `request.user.tenant`, never client input. This
        test proves that even a tenant B admin who already knows tenant A's
        `EmailSettings.id` (e.g. leaked via another channel) cannot reach it:
        the API surface has no id-addressable path at all, and the DB-level
        RLS backstop (proven separately in
        `apps.notifications.tests.test_notifications_rls`-style migration
        tests) still applies if the app layer's tenant-scoped manager were
        ever bypassed.
        """
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        admin_b = _admin(tenant_b)
        row_a = EmailSettings.all_objects.create(tenant=tenant_a, provider="console")

        _login(client, tenant_b, admin_b)
        resp = client.get(URL)
        assert resp.status_code == 200
        assert resp.json().get("id", None) != row_a.id  # id isn't even in fields, but be safe.


class TestEmailSettingsRBAC:
    def test_admin_can_get_and_put(self, client):
        tenant = TenantFactory()
        admin = _admin(tenant)
        _login(client, tenant, admin)

        get_resp = client.get(URL)
        assert get_resp.status_code == 200, get_resp.content

        put_resp = client.put(
            URL,
            data=json.dumps({"provider": "console", "sender_email": "x@y.test", "reply_to": ""}),
            content_type="application/json",
        )
        assert put_resp.status_code == 200, put_resp.content
        assert put_resp.json()["sender_email"] == "x@y.test"

    def test_member_gets_403_on_get_and_put(self, client):
        """`view_key` is deliberately overridden to `TENANT_MANAGE` (same as
        `manage_key`) -- a Member (who lacks `tenant.manage`) must get a
        server-side 403 on GET too, not just PUT."""
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)  # default tenant-wide Member
        _login(client, tenant, member)

        get_resp = client.get(URL)
        assert get_resp.status_code == 403, get_resp.content

        put_resp = client.put(
            URL,
            data=json.dumps({"provider": "console", "sender_email": "x@y.test"}),
            content_type="application/json",
        )
        assert put_resp.status_code == 403, put_resp.content

    def test_member_403_is_a_guessed_url_not_just_ui_gated(self, client):
        """Same assertion as above, phrased as the R4/F1-style "guessed URL"
        proof this repo's convention expects: no UI affordance for this
        screen is rendered to a Member, but the server-side check is what
        actually blocks it if they hit the URL directly."""
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        _login(client, tenant, member)
        resp = client.patch(
            URL, data=json.dumps({"sender_email": "sneaky@y.test"}), content_type="application/json"
        )
        assert resp.status_code == 403
        # And prove the write really never happened.
        with tenant_context(tenant.id):
            row, _ = EmailSettings.objects.get_or_create(tenant=tenant)
        assert row.sender_email != "sneaky@y.test"


class TestEmailSettingsSecretHandling:
    def test_secret_never_round_trips_in_response_body(self, client):
        tenant = TenantFactory()
        admin = _admin(tenant)
        _login(client, tenant, admin)
        secret = "sk-real-secret-value"

        put_resp = client.put(
            URL,
            data=json.dumps(
                {"provider": "brevo", "sender_email": "a@b.test", "api_key": secret}
            ),
            content_type="application/json",
        )
        assert put_resp.status_code == 200, put_resp.content
        put_body = put_resp.json()
        assert put_body["has_api_key"] is True
        assert put_body["api_key_last4"] == secret[-4:]
        assert secret not in put_resp.content.decode()
        assert "api_key" not in put_body  # write-only: never echoed back at all.

        get_resp = client.get(URL)
        assert get_resp.status_code == 200
        get_body = get_resp.json()
        assert get_body["has_api_key"] is True
        assert get_body["api_key_last4"] == secret[-4:]
        assert secret not in get_resp.content.decode()
        assert "api_key" not in get_body

    def test_secret_never_in_audit_log(self, client):
        tenant = TenantFactory()
        admin = _admin(tenant)
        _login(client, tenant, admin)
        secret = "sk-another-real-secret"

        resp = client.put(
            URL,
            data=json.dumps(
                {"provider": "brevo", "sender_email": "a@b.test", "api_key": secret}
            ),
            content_type="application/json",
        )
        assert resp.status_code == 200, resp.content

        entry = AuditLog.all_objects.filter(
            tenant=tenant, action="email_settings.update"
        ).latest("created_at")
        before_str = json.dumps(entry.before)
        after_str = json.dumps(entry.after)
        assert secret not in before_str
        assert secret not in after_str
        assert entry.after["api_key"] in {"unchanged", "updated", "cleared"}
        assert entry.after["api_key"] == "updated"
        # `before` records whether a key was PRESENT prior to this write
        # (never the key material), so it's "present"/"absent", not one of
        # the after-state transition labels.
        assert entry.before["api_key"] in {"present", "absent"}
        assert entry.before["api_key"] == "absent"


class TestEmailSettingsWriteSemantics:
    def test_omitted_api_key_leaves_existing_key_untouched(self, client):
        tenant = TenantFactory()
        admin = _admin(tenant)
        _login(client, tenant, admin)

        first = client.put(
            URL,
            data=json.dumps(
                {"provider": "brevo", "sender_email": "a@b.test", "api_key": "sk-first-value"}
            ),
            content_type="application/json",
        )
        assert first.status_code == 200, first.content
        assert first.json()["api_key_last4"] == "alue"

        second = client.patch(
            URL,
            data=json.dumps({"sender_email": "changed@b.test"}),
            content_type="application/json",
        )
        assert second.status_code == 200, second.content
        body = second.json()
        assert body["sender_email"] == "changed@b.test"
        assert body["has_api_key"] is True
        assert body["api_key_last4"] == "alue"

    def test_blank_api_key_clears_it(self, client):
        tenant = TenantFactory()
        admin = _admin(tenant)
        _login(client, tenant, admin)

        client.put(
            URL,
            data=json.dumps(
                {"provider": "brevo", "sender_email": "a@b.test", "api_key": "sk-to-be-cleared"}
            ),
            content_type="application/json",
        )

        resp = client.patch(URL, data=json.dumps({"api_key": ""}), content_type="application/json")
        assert resp.status_code == 200, resp.content
        body = resp.json()
        assert body["has_api_key"] is False
        assert body["api_key_last4"] == ""

    def test_new_api_key_replaces_last4(self, client):
        tenant = TenantFactory()
        admin = _admin(tenant)
        _login(client, tenant, admin)

        client.put(
            URL,
            data=json.dumps(
                {"provider": "brevo", "sender_email": "a@b.test", "api_key": "sk-original-1111"}
            ),
            content_type="application/json",
        )
        resp = client.patch(
            URL, data=json.dumps({"api_key": "sk-replaced-2222"}), content_type="application/json"
        )
        assert resp.status_code == 200, resp.content
        body = resp.json()
        assert body["has_api_key"] is True
        assert body["api_key_last4"] == "2222"


class TestEmailSettingsSingleton:
    def test_two_gets_create_exactly_one_row(self, client):
        tenant = TenantFactory()
        admin = _admin(tenant)
        _login(client, tenant, admin)

        assert EmailSettings.all_objects.filter(tenant=tenant).count() == 0
        r1 = client.get(URL)
        r2 = client.get(URL)
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert EmailSettings.all_objects.filter(tenant=tenant).count() == 1
