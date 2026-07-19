"""`AuditLog` (`docs/data-model.md` §2, `docs/rbac.md` §5).

A minimal, forward-compatible `AuditLog` is introduced **here, at T1.2**,
rather than waiting for M5 — CLAUDE.md's non-negotiable invariant ("Audit
everything mutating... every... `asset.retire`... writes an immutable
`AuditLog` entry") and this task's own instructions require `asset.retire` to
be audited, and M2/M3's task docs already assume an `AuditLog` exists to
write to (`stock.adjust`, reservation approvals) before M5. **M5
("Finalize the AuditLog") is still the owner of**: the DB-level
immutability trigger (no UPDATE/DELETE, enforced in Postgres, not just by
omitting update/delete views here), the full composite index
`(tenant_id, entity_type, entity_id, created_at)`, RLS, and auditing
`user.manage`/`role.assign`/every other §5 action as those endpoints land.
Nothing here blocks that — this table is additive and its shape matches
`docs/data-model.md` exactly, so T1.3/M5 layer indexes/RLS/triggers on top
without a reshape.

Flagged for db-migration-specialist: this migration introduces a NEW
tenant-owned table (`audit_audit_log`) outside T1.3's originally-scoped
list (`asset`, `asset_field_value`, `attachment`, `tag_link`) — please add
RLS + the composite index here too, and layer the immutability trigger
(M5) on top when M5 lands.
"""

from __future__ import annotations

from django.db import models

from apps.tenancy.models import TenantScopedModel


class AuditLog(TenantScopedModel):
    """Append-only, immutable (docs/data-model.md §2). No `updated_at`
    field on purpose — a row is written once and never modified. Enforcing
    "no UPDATE/DELETE" at the DB level (trigger) is M5's job; the app layer
    already never exposes an update/delete path for this model (no
    serializer/viewset here at all yet — writes only ever go through
    `apps.audit.services.write_audit_log`).
    """

    actor = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_log_entries",
        help_text="Null if the actor's account was later deleted; the log entry itself "
        "is retained (immutable history survives account deletion).",
    )
    action = models.CharField(
        max_length=100, help_text="Permission key or action name, e.g. 'asset.retire'."
    )
    entity_type = models.CharField(max_length=100, help_text="e.g. 'asset'.")
    entity_id = models.CharField(max_length=64)
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "audit_audit_log"
        indexes = [
            models.Index(fields=["tenant", "entity_type", "entity_id", "created_at"]),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.action} {self.entity_type}:{self.entity_id}"
