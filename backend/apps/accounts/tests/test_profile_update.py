"""Acceptance tests for `PATCH /api/v1/me` — the self-service profile (name)
edit behind the Account screen's "Display name" field.

Covers: the happy path and the `/me` response shape it drives (`name` raw vs
`display_name` fallback), the field allowlist (nothing but `name` is
self-settable — notably not `email`, `is_superuser`, or `tenant`), rejection
of an over-long name, the unauthenticated case, and the audit entry.
"""

from __future__ import annotations

import pytest

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.common.tests.factories import DEFAULT_TEST_PASSWORD, TenantFactory, UserFactory
from apps.tenancy.context import tenant_context

pytestmark = pytest.mark.django_db


def _login(client, tenant, user):
    response = client.post(
        "/api/v1/auth/login",
        {"tenant": tenant.slug, "email": user.email, "password": DEFAULT_TEST_PASSWORD},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return response


@pytest.fixture()
def tenant():
    return TenantFactory()


@pytest.fixture()
def user(tenant):
    user = UserFactory(tenant=tenant, name="")
    user.set_password(DEFAULT_TEST_PASSWORD)
    user.save()
    return user


class TestUpdateMe:
    def test_sets_name_and_returns_me_shape(self, client, tenant, user):
        login = _login(client, tenant, user)
        # With no name stored, `display_name` falls back to the email — this
        # is exactly why the Dashboard greeting used to read as an address.
        assert login.json()["name"] == ""
        assert login.json()["display_name"] == user.email

        response = client.patch(
            "/api/v1/me",
            {"name": "Ada Lovelace"},
            content_type="application/json",
        )
        assert response.status_code == 200, response.content
        body = response.json()
        assert body["name"] == "Ada Lovelace"
        assert body["display_name"] == "Ada Lovelace"
        # Full `/me` shape, so the client can refresh from this one response.
        assert body["email"] == user.email
        assert body["tenant"]["slug"] == tenant.slug
        assert "permissions" in body and "memberships" in body

        with tenant_context(tenant.id):
            assert User.all_objects.get(pk=user.pk).name == "Ada Lovelace"

    def test_blank_name_is_allowed_and_falls_back_to_email(self, client, tenant, user):
        _login(client, tenant, user)
        client.patch("/api/v1/me", {"name": "Ada"}, content_type="application/json")

        response = client.patch("/api/v1/me", {"name": ""}, content_type="application/json")
        assert response.status_code == 200, response.content
        assert response.json()["name"] == ""
        assert response.json()["display_name"] == user.email

    def test_ignores_every_field_but_name(self, client, tenant, user):
        _login(client, tenant, user)
        other_tenant = TenantFactory()

        response = client.patch(
            "/api/v1/me",
            {
                "name": "Ada",
                "email": "attacker@evil.test",
                "is_superuser": True,
                "is_staff": True,
                "tenant": other_tenant.id,
            },
            content_type="application/json",
        )
        assert response.status_code == 200, response.content

        with tenant_context(tenant.id):
            refreshed = User.all_objects.get(pk=user.pk)
        assert refreshed.name == "Ada"
        assert refreshed.email == user.email
        assert refreshed.is_superuser is False
        assert refreshed.is_staff is False
        assert refreshed.tenant_id == tenant.id

    def test_rejects_too_long_name(self, client, tenant, user):
        _login(client, tenant, user)
        response = client.patch(
            "/api/v1/me",
            {"name": "x" * 256},
            content_type="application/json",
        )
        assert response.status_code == 400
        assert "name" in response.json()["errors"]

    def test_requires_authentication(self, client):
        response = client.patch(
            "/api/v1/me", {"name": "Nobody"}, content_type="application/json"
        )
        assert response.status_code in (401, 403)

    def test_writes_audit_entry_with_before_after(self, client, tenant, user):
        _login(client, tenant, user)
        client.patch("/api/v1/me", {"name": "Ada"}, content_type="application/json")

        with tenant_context(tenant.id):
            entry = AuditLog.objects.filter(action="user.profile_update").latest("id")
        assert entry.entity_type == "user"
        assert entry.entity_id == str(user.id)  # AuditLog.entity_id is a CharField
        assert entry.actor_id == user.id
        assert entry.before == {"name": ""}
        assert entry.after == {"name": "Ada"}
