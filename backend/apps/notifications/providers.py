"""The `EmailProvider` abstraction (T5.1, `docs/architecture.md` §6).

Business logic (and, later, T5.2's domain-event wiring) NEVER imports
`BrevoProvider` directly -- it only ever talks to the `EmailProvider`
protocol via `get_email_provider()`, which resolves the concrete class from
`settings.NOTIFICATION_EMAIL_PROVIDER` (an env-driven dotted import path).
This mirrors the SAME "dotted class path in env, resolved via
`import_string`" mechanism `config/settings/base.py` already uses for
`STORAGES["default"]["BACKEND"]` (`DJANGO_DEFAULT_FILE_STORAGE`) -- no new
selection mechanism invented here, per this task's instructions.

`ConsoleProvider` is the default everywhere (dev/test/prod, until an
operator explicitly opts into `BrevoProvider` via env once Q6 -- Brevo
sender identity/account/tier, `docs/risks.md` §3 -- is answered). This is a
deliberate fail-safe: no code path can accidentally start sending real
email just because `DEBUG` flips.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from django.conf import settings
from django.utils.module_loading import import_string

logger = logging.getLogger("apps.notifications")


class EmailSendError(Exception):
    """Raised by any `EmailProvider` on a failed send. Treated as transient
    (retryable) by `apps.notifications.tasks.send_transactional_email` --
    there is no permanent/transient distinction yet at T5.1 (flagged for
    T5.2 to refine, e.g. a 4xx "bad template id" from Brevo is arguably not
    worth retrying, whereas a network timeout is)."""


@dataclass
class SendResult:
    """What a successful `send_transactional` call returns."""

    provider_message_id: str
    raw: dict = field(default_factory=dict)


class EmailProvider(Protocol):
    """`docs/architecture.md` §6's interface. Business logic emits domain
    events that map to templated emails through THIS protocol only."""

    def send_transactional(
        self,
        *,
        template_id: str,
        to: str,
        params: dict[str, Any],
        tags: Optional[list[str]] = None,
    ) -> SendResult:
        """Send one templated transactional email. Raises `EmailSendError`
        on any failure. Must never block longer than a normal HTTP call --
        callers are always inside a Celery task (`apps.notifications.
        tasks.send_transactional_email`), never the request/response cycle."""
        ...


class ConsoleProvider:
    """Dev/test implementation (`docs/architecture.md` §6): logs the
    would-be send instead of calling any real API. Active by default in
    every environment (see module docstring) until an operator opts into
    `BrevoProvider`."""

    def send_transactional(
        self,
        *,
        template_id: str,
        to: str,
        params: dict[str, Any],
        tags: Optional[list[str]] = None,
    ) -> SendResult:
        message_id = f"console-{uuid.uuid4()}"
        logger.info(
            "ConsoleProvider: would send template_id=%s to=%s tags=%s params=%s message_id=%s",
            template_id,
            to,
            tags or [],
            params,
            message_id,
        )
        return SendResult(provider_message_id=message_id, raw={"console": True})


class BrevoProvider:
    """Brevo transactional HTTP API (`docs/architecture.md` §6: "recommend
    the transactional API" over SMTP). **Implemented but deliberately not
    exercised in tests/dev** -- Q6 (sender identity/account/tier,
    `docs/risks.md` §3) is unanswered, so there is no real `BREVO_API_KEY`
    to send with yet. Selecting this provider without a valid key simply
    makes every send raise `EmailSendError` (a non-2xx/network failure),
    which the Celery task retries and eventually logs `failed` -- the same
    fail-closed behavior as any other transient outage.

    Uses `urllib` (stdlib) rather than adding a new HTTP client dependency
    for a code path that cannot be exercised yet; swapping to `requests`/
    `httpx` later is a pure internal refactor, no interface change.
    """

    API_URL = "https://api.brevo.com/v3/smtp/email"

    def __init__(self) -> None:
        # Read ONLY from settings (itself env-only, 12-factor) -- never
        # hardcoded, never logged (see `send_transactional` below).
        self._api_key = getattr(settings, "BREVO_API_KEY", "") or ""
        self._sender = getattr(settings, "BREVO_SENDER_EMAIL", "") or ""

    def send_transactional(
        self,
        *,
        template_id: str,
        to: str,
        params: dict[str, Any],
        tags: Optional[list[str]] = None,
    ) -> SendResult:
        if not self._api_key:
            # Fail closed rather than making a doomed anonymous API call --
            # still surfaces as `EmailSendError` -> retry -> `failed`, same
            # contract as any other send failure.
            raise EmailSendError(
                "BrevoProvider selected but BREVO_API_KEY is not set (Q6 unanswered, "
                "docs/risks.md §3) -- refusing to attempt a send."
            )

        payload: dict[str, Any] = {
            "templateId": template_id,
            "to": [{"email": to}],
            "params": params,
        }
        if self._sender:
            payload["sender"] = {"email": self._sender}
        if tags:
            payload["tags"] = tags

        request = urllib.request.Request(
            self.API_URL,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "api-key": self._api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            # Never log `self._api_key` or the raw request/response body --
            # only the outcome and, on failure, the exception's message.
            with urllib.request.urlopen(request, timeout=10) as response:
                body = json.loads(response.read().decode("utf-8") or "{}")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as exc:
            raise EmailSendError(f"Brevo send failed: {exc}") from exc

        message_id = body.get("messageId") or ""
        return SendResult(provider_message_id=message_id, raw=body)


def get_email_provider() -> EmailProvider:
    """Resolve the configured `EmailProvider` from
    `settings.NOTIFICATION_EMAIL_PROVIDER` (dotted import path, env-driven --
    same mechanism as `STORAGES["default"]["BACKEND"]`). A fresh instance is
    returned on every call (construction is cheap; avoids any surprising
    cross-request/cross-task shared state)."""
    provider_class = import_string(settings.NOTIFICATION_EMAIL_PROVIDER)
    return provider_class()
