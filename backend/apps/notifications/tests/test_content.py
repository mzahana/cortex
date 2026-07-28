"""Unit tests for `apps.notifications.content.build_email_content` -- the
local HTML/text builder that replaced the removed Brevo dashboard
`templateId` system (see `providers.py`/`content.py` module docstrings).

Covers all 6 slugs Cortex actually sends (`apps.notifications.receivers`/
`tasks`/`apps.accounts.api`) plus the fail-closed "unknown slug" case.
"""

from __future__ import annotations

import pytest

from apps.notifications.content import build_email_content

# No DB needed -- `build_email_content` is a pure function.


class TestReservationConfirmed:
    def test_interpolates_params(self):
        subject, html, text = build_email_content(
            "reservation-confirmed",
            {
                "asset_name": "Drone A",
                "start_at": "2026-08-01T10:00:00+00:00",
                "end_at": "2026-08-01T12:00:00+00:00",
            },
        )
        assert subject
        assert html
        assert "Drone A" in subject
        assert "Drone A" in html
        # Formatted for readability (not the raw ISO string) with an explicit
        # UTC label, since the email can't know the recipient's timezone the
        # way the app's own UI (browser-local `toLocaleTimeString`) can.
        assert "Aug 01, 2026, 10:00 UTC" in html
        assert "Aug 01, 2026, 12:00 UTC" in html
        assert "Drone A" in text

    def test_falls_back_to_asset_id_when_name_missing(self):
        subject, html, text = build_email_content(
            "reservation-confirmed", {"asset_id": 42, "start_at": "", "end_at": ""}
        )
        assert "asset #42" in subject
        assert "asset #42" in html


class TestApprovalRequest:
    def test_interpolates_requester_and_asset_names(self):
        subject, html, text = build_email_content(
            "approval-request",
            {
                "asset_name": "GPU Server",
                "requester_name": "Ada Lovelace",
                "start_at": "S",
                "end_at": "E",
            },
        )
        assert "GPU Server" in subject
        assert "Ada Lovelace" in subject
        assert "GPU Server" in html
        assert "Ada Lovelace" in html
        assert "GPU Server" in text
        assert "Ada Lovelace" in text

    def test_falls_back_to_ids_when_names_missing(self):
        subject, html, text = build_email_content(
            "approval-request",
            {"asset_id": 7, "requester_id": 9, "start_at": "S", "end_at": "E"},
        )
        assert "asset #7" in subject
        assert "user #9" in subject


class TestApprovalDecision:
    def test_approved_outcome(self):
        subject, html, text = build_email_content(
            "approval-decision", {"asset_name": "Soldering Iron", "approved": True}
        )
        assert "approved" in subject.lower()
        assert "Soldering Iron" in subject
        assert "approved" in html.lower()
        assert "approved" in text.lower()

    def test_declined_outcome_includes_note(self):
        subject, html, text = build_email_content(
            "approval-decision",
            {"asset_name": "Soldering Iron", "approved": False, "note": "Already booked"},
        )
        assert "declined" in subject.lower()
        assert "Already booked" in html
        assert "Already booked" in text

    def test_no_note_omits_note_section(self):
        subject, html, text = build_email_content(
            "approval-decision", {"asset_name": "X", "approved": True, "note": ""}
        )
        assert "Note:" not in html
        assert "Note:" not in text


class TestOverdueReminder:
    def test_interpolates_asset_and_due_date(self):
        subject, html, text = build_email_content(
            "overdue-reminder", {"asset_name": "Oscilloscope", "due_at": "2026-07-01T00:00:00Z"}
        )
        assert "Oscilloscope" in subject
        assert "Oscilloscope" in html
        assert "Jul 01, 2026, 00:00 UTC" in html
        assert "Oscilloscope" in text


class TestLowStockAlert:
    def test_interpolates_quantity_and_threshold(self):
        subject, html, text = build_email_content(
            "low-stock-alert",
            {"asset_name": "M3 Screws", "new_quantity": 2, "reorder_threshold": 10},
        )
        assert "M3 Screws" in subject
        assert "M3 Screws" in html
        assert "2" in html
        assert "10" in html
        assert "M3 Screws" in text

    def test_falls_back_to_quantity_on_hand_key(self):
        subject, html, text = build_email_content(
            "low-stock-alert",
            {"asset_name": "Bolts", "quantity_on_hand": 3, "reorder_threshold": 5},
        )
        assert "3" in html


class TestPasswordReset:
    def test_interpolates_name_and_reset_url(self):
        subject, html, text = build_email_content(
            "password-reset",
            {
                "name": "Grace Hopper",
                "reset_url": "https://example.test/reset/abc",
                "tenant_name": "Acme Lab",
            },
        )
        assert "Acme Lab" in subject
        assert "Grace Hopper" in html
        assert "https://example.test/reset/abc" in html
        assert "https://example.test/reset/abc" in text

    def test_missing_name_uses_generic_greeting(self):
        subject, html, text = build_email_content(
            "password-reset", {"reset_url": "https://example.test/reset/xyz"}
        )
        assert "Hi," in html
        assert "https://example.test/reset/xyz" in html


class TestUnknownSlug:
    def test_raises_key_error(self):
        with pytest.raises(KeyError):
            build_email_content("not-a-real-slug", {})


class TestDateTimeFormatting:
    """`start_at`/`end_at`/`due_at` arrive as UTC `isoformat()` strings.
    The email must format them readably with an explicit UTC label -- a
    bare `2026-07-30T09:00:00+00:00` reads as "wrong" next to the app's own
    UI, which renders the same instant converted to the viewer's browser
    timezone."""

    def test_reservation_confirmed_labels_utc_explicitly(self):
        _subject, html, text = build_email_content(
            "reservation-confirmed",
            {
                "asset_name": "Drone A",
                "start_at": "2026-07-30T09:00:00+00:00",
                "end_at": "2026-07-30T13:00:00+00:00",
            },
        )
        assert "Jul 30, 2026, 09:00 UTC" in html
        assert "Jul 30, 2026, 13:00 UTC" in html
        assert "09:00:00+00:00" not in html
        assert "UTC" in text

    def test_unparseable_value_falls_back_to_raw_string(self):
        _subject, html, _text = build_email_content(
            "reservation-confirmed", {"asset_name": "Drone A", "start_at": "S", "end_at": "E"}
        )
        assert "S" in html
        assert "E" in html

    def test_missing_value_renders_empty(self):
        _subject, html, _text = build_email_content(
            "reservation-confirmed", {"asset_name": "Drone A", "start_at": "", "end_at": ""}
        )
        assert "Start:</strong> <br>" in html or "Start:</strong> <" in html


class TestHtmlEscaping:
    """`params` values (asset names, requester names, approval notes) are
    tenant-user-controlled -- any Member who can name an asset or write an
    approval note. They must never land unescaped in the HTML body, or one
    user could inject markup into mail delivered to another user's inbox."""

    MARKUP = '</p><a href="https://evil.example/login">Re-authenticate</a><p>'

    def test_asset_name_is_escaped_in_approval_request(self):
        _subject, html, _text = build_email_content(
            "approval-request",
            {"asset_name": self.MARKUP, "requester_name": "Ada", "start_at": "S", "end_at": "E"},
        )
        assert "<a href=" not in html
        assert "&lt;a href=" in html

    def test_requester_name_is_escaped_in_approval_request(self):
        _subject, html, _text = build_email_content(
            "approval-request",
            {
                "asset_name": "GPU Server",
                "requester_name": self.MARKUP,
                "start_at": "S",
                "end_at": "E",
            },
        )
        assert "<a href=" not in html
        assert "&lt;a href=" in html

    def test_note_is_escaped_in_approval_decision(self):
        _subject, html, _text = build_email_content(
            "approval-decision",
            {"asset_name": "Soldering Iron", "approved": False, "note": self.MARKUP},
        )
        assert "<a href=" not in html
        assert "&lt;a href=" in html

    def test_asset_name_is_escaped_in_reservation_confirmed(self):
        _subject, html, _text = build_email_content(
            "reservation-confirmed",
            {"asset_name": self.MARKUP, "start_at": "S", "end_at": "E"},
        )
        assert "<a href=" not in html
        assert "&lt;a href=" in html

    def test_tenant_name_is_escaped_in_password_reset(self):
        # password-reset legitimately contains a real `<a href=...>` link
        # (the reset URL), so assert on the injected markup specifically
        # rather than the mere absence of any anchor tag.
        _subject, html, _text = build_email_content(
            "password-reset",
            {
                "name": "Grace",
                "reset_url": "https://example.test/reset/abc",
                "tenant_name": self.MARKUP,
            },
        )
        assert self.MARKUP not in html
        assert "&lt;a href=" in html

    def test_subject_strips_embedded_newlines(self):
        subject, _html, _text = build_email_content(
            "approval-request",
            {
                "asset_name": "GPU Server",
                "requester_name": "Ada\r\nBcc: attacker@evil.example",
                "start_at": "S",
                "end_at": "E",
            },
        )
        assert "\n" not in subject
        assert "\r" not in subject
