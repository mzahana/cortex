"""`EmailLog` and `NotificationPref` (T5.1, `docs/data-model.md` "Audit,
notifications, email" section; `docs/architecture.md` §6).

Both are tenant-owned -> `TenantScopedModel` (T0.4's fail-closed base
manager), same golden-path rule as every other app (`apps.stock`,
`apps.reservations`). **RLS + the composite index + any DB-level
immutability trigger for these tables are T5.4's job (db-migration-
specialist)** -- this task only defines the shape; see the migration
docstring for the exact split (mirrors `apps.audit`'s own
`0001_initial.py` (plain `CreateModel`) / `0002_rls_policies.py` (RLS,
added separately by db-migration-specialist) precedent).

## Field-naming note (data-model.md vs. this task's instructions)

`docs/data-model.md`'s `EmailLog` row lists `user_id`; this task's own
instructions list `recipient`. Both are kept: `user` is the internal
`accounts.User` the email was sent to (nullable -- an email can target an
address that isn't tied to a live account, e.g. before T5.2 wires real
recipients), and `recipient` is the actual email address string used for
THIS send, captured at send time so the log stays historically accurate
even if the user's `email` field changes later.
"""

from __future__ import annotations

from django.db import models

from apps.tenancy.models import TenantScopedModel


class EmailLog(TenantScopedModel):
    """One row per attempted transactional email send. Written by
    `apps.notifications.services.enqueue_transactional_email` (status
    `queued`) and updated by `apps.notifications.tasks.
    send_transactional_email` (`sent`/`failed` on completion; `bounced` is
    reserved for a future Brevo webhook, not produced by this task).
    """

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        BOUNCED = "bounced", "Bounced"

    user = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="email_logs",
        help_text="The account this email was sent to, if any (see module docstring).",
    )
    recipient = models.EmailField(help_text="The actual address used for this send.")
    event_type = models.CharField(
        max_length=100,
        help_text="Domain event key, e.g. 'low_stock_crossed', 'reservation_confirmed'.",
    )
    provider = models.CharField(
        max_length=32, help_text="Provider class name used for this send, e.g. 'ConsoleProvider'."
    )
    provider_message_id = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.QUEUED)
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "notifications_email_log"
        indexes = [
            models.Index(fields=["tenant", "event_type", "created_at"]),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.event_type}:{self.recipient}:{self.status}"


class NotificationPref(TenantScopedModel):
    """Per user x event_type gate on OPTIONAL email sends
    (`docs/data-model.md`). Security-critical/transactional emails (none
    identified yet at T5.1 -- flagged for T5.2 to confirm as the real event
    catalog lands) must always send regardless of this table; see
    `apps.notifications.services.enqueue_transactional_email`'s
    `optional` flag.
    """

    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="notification_prefs",
    )
    event_type = models.CharField(max_length=100)
    email_enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "notifications_notification_pref"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "event_type"], name="notif_pref_unique_user_event"
            )
        ]
        indexes = [
            models.Index(fields=["tenant", "user"]),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.user_id}:{self.event_type}:{self.email_enabled}"
