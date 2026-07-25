"""`get_email_provider()` resolution-priority tests, and `crypto.py`'s
round-trip/empty-input behavior, for the per-tenant `EmailSettings` feature.

These are the regression-safety-critical cases: this feature must be a
zero-behavior-change no-op for every tenant that has never configured the UI
(no tenant context, or tenant context but no row -> exactly today's
env-driven fallback), and the per-tenant row must take priority over the env
default the moment it exists -- proven explicitly even when the env default
disagrees with the row (env says Brevo, row says console -> console wins).
"""

from __future__ import annotations

import pytest
from django.test import override_settings

from apps.common.tests.factories import TenantFactory
from apps.notifications.crypto import decrypt_api_key, encrypt_api_key
from apps.notifications.models import EmailSettings
from apps.notifications.providers import BrevoProvider, ConsoleProvider, get_email_provider
from apps.tenancy.context import tenant_context

pytestmark = pytest.mark.django_db


class TestGetEmailProviderFallback:
    def test_no_tenant_context_falls_back_to_env_setting(self):
        # No `tenant_context()` entered at all -> `get_current_tenant_id()`
        # is None -> exactly today's `import_string(NOTIFICATION_EMAIL_PROVIDER)`
        # behavior, unchanged by this feature's existence.
        with override_settings(
            NOTIFICATION_EMAIL_PROVIDER="apps.notifications.providers.ConsoleProvider"
        ):
            provider = get_email_provider()
        assert isinstance(provider, ConsoleProvider)

    def test_tenant_context_but_no_row_falls_back_to_env_setting(self):
        tenant = TenantFactory()
        # Deliberately no `EmailSettings` row created for this tenant -- proves
        # `get_email_provider()` only ever does a `.filter(...).first()` read,
        # never an implicit `get_or_create` of its own (that would be a
        # surprising side effect of merely resolving a provider instance).
        with override_settings(
            NOTIFICATION_EMAIL_PROVIDER="apps.notifications.providers.ConsoleProvider"
        ):
            with tenant_context(tenant.id):
                provider = get_email_provider()
                assert EmailSettings.objects.filter(tenant=tenant).count() == 0
        assert isinstance(provider, ConsoleProvider)


class TestGetEmailProviderPerTenantOverride:
    def test_brevo_row_returns_brevo_provider_with_decrypted_key(self):
        tenant = TenantFactory()
        with tenant_context(tenant.id):
            EmailSettings.objects.create(
                tenant=tenant,
                provider=EmailSettings.Provider.BREVO,
                sender_email="row-sender@tenant.test",
                reply_to="row-reply@tenant.test",
                api_key_encrypted=encrypt_api_key("row-real-key"),
                api_key_last4="-key",
            )
            with override_settings(BREVO_API_KEY="env-should-not-be-used"):
                provider = get_email_provider()

        assert isinstance(provider, BrevoProvider)
        # Reflects the ROW's decrypted values, never `settings.BREVO_API_KEY`.
        assert provider._api_key == "row-real-key"
        assert provider._sender == "row-sender@tenant.test"
        assert provider._reply_to == "row-reply@tenant.test"

    def test_console_row_wins_over_env_default_even_when_env_says_brevo(self):
        tenant = TenantFactory()
        with tenant_context(tenant.id):
            EmailSettings.objects.create(tenant=tenant, provider=EmailSettings.Provider.CONSOLE)
            with override_settings(
                NOTIFICATION_EMAIL_PROVIDER="apps.notifications.providers.BrevoProvider"
            ):
                provider = get_email_provider()

        # The per-tenant row (console) takes priority over the env default
        # (brevo) -- proves row-priority, not just "row exists at all".
        assert isinstance(provider, ConsoleProvider)


class TestCryptoRoundTrip:
    def test_round_trip(self):
        assert decrypt_api_key(encrypt_api_key("some-key")) == "some-key"

    def test_empty_string_round_trips_without_calling_fernet(self):
        # `encrypt_api_key("")` must short-circuit to `b""` directly, never
        # passed to Fernet (which raises on an empty token / is pointless
        # ciphertext for "nothing").
        assert encrypt_api_key("") == b""
        assert decrypt_api_key(b"") == ""
        assert decrypt_api_key(encrypt_api_key("")) == ""
