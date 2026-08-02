"""`BrevoProvider.send_transactional`'s locally-built payload
(`apps.notifications.content.build_email_content`) -- replaces the removed
Brevo dashboard `templateId` mapping (`BREVO_TEMPLATE_IDS`) entirely.

These tests never hit the network: `urlopen` is monkeypatched so a
"success" case only proves the OUTGOING payload is correct (`subject` +
`htmlContent`, and explicitly NO `templateId` key anywhere), and the
"no api key"/"no sender" cases prove no HTTP call is even attempted.
"""

from __future__ import annotations

import json
import urllib.request
from contextlib import contextmanager
from typing import Any

import pytest
from django.test import override_settings

from apps.notifications.providers import BrevoProvider, EmailSendError

pytestmark = pytest.mark.django_db


class _FakeResponse:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@contextmanager
def _capture_request(monkeypatch, response_body=None):
    captured = {}

    def fake_urlopen(request, timeout=10):
        captured["request"] = request
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(response_body or {"messageId": "fake-message-id"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    yield captured


class TestSendTransactionalPayloadShape:
    def test_posts_subject_and_html_content_with_no_template_id(self, monkeypatch):
        with override_settings(
            BREVO_API_KEY="fake-key",
            BREVO_SENDER_EMAIL="sender@example.com",
        ):
            provider = BrevoProvider()
            with _capture_request(monkeypatch) as captured:
                result = provider.send_transactional(
                    template_id="reservation-confirmed",
                    to="user@example.com",
                    params={
                        "asset_name": "Drone A",
                        "start_at": "2026-08-01T10:00:00+00:00",
                        "end_at": "2026-08-01T12:00:00+00:00",
                    },
                )

        payload = captured["payload"]
        assert "templateId" not in payload
        assert payload["subject"]
        assert "Drone A" in payload["subject"]
        assert payload["htmlContent"]
        assert "Drone A" in payload["htmlContent"]
        assert payload["textContent"]
        assert payload["to"] == [{"email": "user@example.com"}]
        assert payload["sender"] == {"email": "sender@example.com"}
        assert result.provider_message_id == "fake-message-id"

    def test_every_known_slug_builds_a_valid_payload(self, monkeypatch):
        """All 6 real slugs must produce a payload with no templateId --
        proves the removal is complete across every event type this app
        actually sends, not just the one exercised above."""
        cases: dict[str, dict[str, Any]] = {
            "reservation-confirmed": {"asset_name": "A", "start_at": "S", "end_at": "E"},
            "approval-request": {
                "asset_name": "A",
                "requester_name": "R",
                "start_at": "S",
                "end_at": "E",
            },
            "approval-decision": {"asset_name": "A", "approved": True},
            "overdue-reminder": {"asset_name": "A", "due_at": "D"},
            "low-stock-alert": {"asset_name": "A", "new_quantity": 1, "reorder_threshold": 5},
            "password-reset": {"reset_url": "https://example.test/reset"},
        }
        with override_settings(BREVO_API_KEY="fake-key", BREVO_SENDER_EMAIL="sender@example.com"):
            provider = BrevoProvider()
            for template_id, params in cases.items():
                with _capture_request(monkeypatch) as captured:
                    provider.send_transactional(
                        template_id=template_id, to="user@example.com", params=params
                    )
                payload = captured["payload"]
                assert "templateId" not in payload, template_id
                assert payload["subject"], template_id
                assert payload["htmlContent"], template_id

    def test_unknown_internal_slug_raises_without_http_call(self, monkeypatch):
        called = False

        def fake_urlopen(*args, **kwargs):
            nonlocal called
            called = True
            raise AssertionError("must not attempt HTTP call for an unknown internal slug")

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        with override_settings(BREVO_API_KEY="fake-key", BREVO_SENDER_EMAIL="sender@example.com"):
            provider = BrevoProvider()
            with pytest.raises(EmailSendError, match="not-a-real-slug"):
                provider.send_transactional(
                    template_id="not-a-real-slug",
                    to="user@example.com",
                    params={},
                )

        assert called is False

    def test_missing_api_key_raises_without_http_call(self, monkeypatch):
        called = False

        def fake_urlopen(*args, **kwargs):
            nonlocal called
            called = True
            raise AssertionError("must not attempt HTTP call with no API key")

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        with override_settings(BREVO_API_KEY="", BREVO_SENDER_EMAIL="sender@example.com"):
            provider = BrevoProvider()
            with pytest.raises(EmailSendError, match="BREVO_API_KEY"):
                provider.send_transactional(
                    template_id="password-reset",
                    to="user@example.com",
                    params={"reset_url": "https://example.test"},
                )

        assert called is False

    def test_missing_sender_raises_without_http_call(self, monkeypatch):
        called = False

        def fake_urlopen(*args, **kwargs):
            nonlocal called
            called = True
            raise AssertionError("must not attempt HTTP call with no sender configured")

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        with override_settings(BREVO_API_KEY="fake-key", BREVO_SENDER_EMAIL=""):
            provider = BrevoProvider()
            with pytest.raises(EmailSendError, match="sender"):
                provider.send_transactional(
                    template_id="password-reset",
                    to="user@example.com",
                    params={"reset_url": "https://example.test"},
                )

        assert called is False

    def test_reply_to_and_tags_are_included_when_set(self, monkeypatch):
        with override_settings(
            BREVO_API_KEY="fake-key",
            BREVO_SENDER_EMAIL="sender@example.com",
            BREVO_REPLY_TO="reply@example.com",
        ):
            provider = BrevoProvider()
            with _capture_request(monkeypatch) as captured:
                provider.send_transactional(
                    template_id="password-reset",
                    to="user@example.com",
                    params={"reset_url": "https://example.test"},
                    tags=["password-reset"],
                )

        payload = captured["payload"]
        assert payload["replyTo"] == {"email": "reply@example.com"}
        assert payload["tags"] == ["password-reset"]


class TestBrevoSendTestEmail:
    """`send_test_email` -- the "Send test email" button's provider call.
    Deliberately bypasses the templated-content system entirely (hardcoded
    subject/body): an admin verifying their API key/sender shouldn't need
    any notification-type configuration at all."""

    def test_sends_plain_content_with_no_template_id(self, monkeypatch):
        with override_settings(
            BREVO_API_KEY="fake-key",
            BREVO_SENDER_EMAIL="sender@example.com",
        ):
            provider = BrevoProvider()
            with _capture_request(monkeypatch) as captured:
                result = provider.send_test_email(
                    to="admin@example.com",
                    tenant_name="Acme Lab",
                    triggered_by="Ada Admin",
                )

        payload = captured["payload"]
        assert "templateId" not in payload
        assert payload["to"] == [{"email": "admin@example.com"}]
        assert payload["sender"] == {"email": "sender@example.com"}
        assert "subject" in payload and payload["subject"]
        assert "htmlContent" in payload
        assert "Acme Lab" in payload["htmlContent"]
        assert "Ada Admin" in payload["htmlContent"]
        assert result.provider_message_id == "fake-message-id"

    def test_tenant_name_and_triggered_by_are_escaped(self, monkeypatch):
        # tenant_name/triggered_by are admin/user-controlled display strings
        # (tenant settings, account name) -- must never land unescaped in
        # the HTML body sent to the admin's own inbox.
        with override_settings(
            BREVO_API_KEY="fake-key",
            BREVO_SENDER_EMAIL="sender@example.com",
        ):
            provider = BrevoProvider()
            with _capture_request(monkeypatch) as captured:
                provider.send_test_email(
                    to="admin@example.com",
                    tenant_name='</p><a href="https://evil.example">x</a><p>',
                    triggered_by='</p><a href="https://evil.example">y</a><p>',
                )

        html_content = captured["payload"]["htmlContent"]
        assert "<a href=" not in html_content
        assert "&lt;a href=" in html_content

    def test_missing_api_key_raises_without_http_call(self, monkeypatch):
        called = False

        def fake_urlopen(*args, **kwargs):
            nonlocal called
            called = True
            raise AssertionError("must not attempt HTTP call with no API key")

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        with override_settings(BREVO_API_KEY="", BREVO_SENDER_EMAIL="sender@example.com"):
            provider = BrevoProvider()
            with pytest.raises(EmailSendError, match="BREVO_API_KEY"):
                provider.send_test_email(
                    to="admin@example.com", tenant_name="Acme", triggered_by="Ada"
                )

        assert called is False

    def test_missing_sender_raises_without_http_call(self, monkeypatch):
        called = False

        def fake_urlopen(*args, **kwargs):
            nonlocal called
            called = True
            raise AssertionError("must not attempt HTTP call with no sender configured")

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        with override_settings(BREVO_API_KEY="fake-key", BREVO_SENDER_EMAIL=""):
            provider = BrevoProvider()
            with pytest.raises(EmailSendError, match="sender"):
                provider.send_test_email(
                    to="admin@example.com", tenant_name="Acme", triggered_by="Ada"
                )

        assert called is False
