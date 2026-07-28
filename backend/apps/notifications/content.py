"""Built-in email content for `BrevoProvider.send_transactional` (replaces
the earlier Brevo-dashboard-template-id system: a non-technical admin
should never have to configure a numeric Brevo template id per notification
type just to get any transactional email sending at all -- see the product
decision recorded on the removal commit).

Every notification type Cortex actually sends (`apps.notifications.
receivers`/`tasks`/`apps.accounts.api`'s 6 `template_id=` slugs) gets a
small, static subject + HTML body built HERE, locally, from the `params`
dict already passed to `send_transactional` -- no Brevo dashboard template,
no per-tenant/global id mapping, nothing to configure beyond the API key +
sender address (`BrevoProvider.send_test_email` already proves that much
alone is enough to send).

`build_email_content(template_id, params)` returns `(subject, html, text)`.
`text` is a plain-text fallback derived from stripping the HTML down to
recognizable line-oriented content -- Brevo accepts an inline `textContent`
alongside `htmlContent`; if omitted, most clients happily render the HTML
part only, but a text part is a widely-accepted best practice and costs
nothing to include.

An unrecognized `template_id` (a slug the codebase doesn't actually use)
raises `KeyError` -- `BrevoProvider.send_transactional` turns that into an
`EmailSendError`, the same fail-closed contract every other invalid-input
case already has in this module.
"""

from __future__ import annotations

import html as _html_lib
from typing import Any, Callable

from django.utils.dateparse import parse_datetime


def _fmt_dt(value: Any) -> str:
    """Format an ISO-8601 UTC timestamp for email display.

    `start_at`/`end_at`/`due_at` arrive as UTC `isoformat()` strings (e.g.
    `2026-07-30T09:00:00+00:00`). The app's own UI renders these converted
    to the *viewer's browser* timezone (`ReservationListItem.tsx`'s
    `toLocaleTimeString`), which an email can't replicate server-side (no
    recipient timezone is known/stored -- there is no per-tenant timezone
    field in the data model). Rather than print a bare `09:00:00+00:00`
    that reads as "wrong" next to the UI's locally-converted time, spell out
    the UTC label explicitly so the offset is never ambiguous.
    """
    if not value:
        return ""
    parsed = parse_datetime(str(value))
    if parsed is None:
        return str(value)
    return parsed.strftime("%b %d, %Y, %H:%M UTC")


def _esc(value: Any) -> str:
    """Escape a value for safe interpolation into an HTML email body.

    `params` values (asset names, user display names, approval notes) are
    tenant-user-controlled -- e.g. any Member who can name an asset or write
    an approval note. Without escaping, a name/note containing HTML markup
    would be interpolated verbatim into another user's inbox, letting one
    tenant user inject content into mail delivered from the tenant's own
    verified sender. Every value must go through this before landing in an
    `html` string.
    """
    return _html_lib.escape(str(value), quote=True)


def _subject(value: str) -> str:
    """Strip newlines from subject-line text to rule out header injection."""
    return " ".join(value.split())


def _reservation_confirmed(params: dict[str, Any]) -> tuple[str, str, str]:
    asset_name = _esc(params.get("asset_name") or f"asset #{params.get('asset_id')}")
    start_at = _esc(_fmt_dt(params.get("start_at")))
    end_at = _esc(_fmt_dt(params.get("end_at")))
    subject = _subject(f"Reservation confirmed: {asset_name}")
    html = (
        f"<p>Your reservation for <strong>{asset_name}</strong> is confirmed.</p>"
        f"<p><strong>Start:</strong> {start_at}<br>"
        f"<strong>End:</strong> {end_at}</p>"
    )
    text = f"Your reservation for {asset_name} is confirmed.\nStart: {start_at}\nEnd: {end_at}"
    return subject, html, text


def _approval_request(params: dict[str, Any]) -> tuple[str, str, str]:
    asset_name = _esc(params.get("asset_name") or f"asset #{params.get('asset_id')}")
    requester_name = _esc(params.get("requester_name") or f"user #{params.get('requester_id')}")
    start_at = _esc(_fmt_dt(params.get("start_at")))
    end_at = _esc(_fmt_dt(params.get("end_at")))
    subject = _subject(f"Approval needed: {asset_name} requested by {requester_name}")
    html = (
        f"<p><strong>{requester_name}</strong> has requested "
        f"<strong>{asset_name}</strong> and is waiting for your approval.</p>"
        f"<p><strong>Start:</strong> {start_at}<br>"
        f"<strong>End:</strong> {end_at}</p>"
    )
    text = (
        f"{requester_name} has requested {asset_name} and is waiting for your approval.\n"
        f"Start: {start_at}\nEnd: {end_at}"
    )
    return subject, html, text


def _approval_decision(params: dict[str, Any]) -> tuple[str, str, str]:
    asset_name = _esc(params.get("asset_name") or f"asset #{params.get('asset_id')}")
    approved = bool(params.get("approved"))
    outcome = "approved" if approved else "declined"
    note = _esc(params.get("note") or "")
    subject = _subject(f"Reservation {outcome}: {asset_name}")
    html = (
        f"<p>Your reservation for <strong>{asset_name}</strong> has been "
        f"<strong>{outcome}</strong>.</p>"
    )
    if note:
        html += f"<p><strong>Note:</strong> {note}</p>"
    text = f"Your reservation for {asset_name} has been {outcome}."
    if note:
        text += f"\nNote: {note}"
    return subject, html, text


def _overdue_reminder(params: dict[str, Any]) -> tuple[str, str, str]:
    asset_name = _esc(params.get("asset_name") or f"asset #{params.get('asset_id')}")
    due_at = _esc(_fmt_dt(params.get("due_at")))
    subject = _subject(f"Overdue: {asset_name}")
    html = (
        f"<p><strong>{asset_name}</strong> was due back on <strong>{due_at}</strong> "
        "and is still checked out. Please return it as soon as possible.</p>"
    )
    text = f"{asset_name} was due back on {due_at} and is still checked out. Please return it."
    return subject, html, text


def _low_stock_alert(params: dict[str, Any]) -> tuple[str, str, str]:
    asset_name = _esc(params.get("asset_name") or f"asset #{params.get('asset_id')}")
    quantity = _esc(params.get("new_quantity", params.get("quantity_on_hand", "")))
    threshold = _esc(params.get("reorder_threshold", ""))
    subject = _subject(f"Low stock: {asset_name}")
    html = (
        f"<p><strong>{asset_name}</strong> is running low: "
        f"<strong>{quantity}</strong> on hand, at or below the reorder threshold of "
        f"<strong>{threshold}</strong>.</p>"
    )
    text = f"{asset_name} is running low: {quantity} on hand (reorder threshold {threshold})."
    return subject, html, text


def _password_reset(params: dict[str, Any]) -> tuple[str, str, str]:
    name = _esc(params.get("name") or "")
    reset_url = _esc(params.get("reset_url", ""))
    tenant_name = _esc(params.get("tenant_name") or "Cortex")
    greeting = f"Hi {name}," if name else "Hi,"
    subject = _subject(f"Reset your {tenant_name} password")
    html = (
        f"<p>{greeting}</p>"
        f"<p>We received a request to reset your <strong>{tenant_name}</strong> password. "
        f'Click the link below to choose a new one:</p><p><a href="{reset_url}">{reset_url}</a></p>'
        "<p>If you didn't request this, you can safely ignore this email.</p>"
    )
    text = (
        f"{greeting}\n\nWe received a request to reset your {tenant_name} password. "
        f"Use this link to choose a new one:\n{reset_url}\n\n"
        "If you didn't request this, you can safely ignore this email."
    )
    return subject, html, text


_BUILDERS: dict[str, Callable[[dict[str, Any]], tuple[str, str, str]]] = {
    "password-reset": _password_reset,
    "overdue-reminder": _overdue_reminder,
    "low-stock-alert": _low_stock_alert,
    "reservation-confirmed": _reservation_confirmed,
    "approval-request": _approval_request,
    "approval-decision": _approval_decision,
}


def build_email_content(template_id: str, params: dict[str, Any]) -> tuple[str, str, str]:
    """Return `(subject, html_content, text_content)` for the given internal
    slug. Raises `KeyError` for any slug this app doesn't actually send --
    callers (`BrevoProvider.send_transactional`) turn that into a fail-closed
    `EmailSendError`, same posture as the removed template-id mapping."""
    builder = _BUILDERS[template_id]
    return builder(params)
