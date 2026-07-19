"""Write path for `AuditLog` (T1.2, see `models.py` docstring for scope).

`write_audit_log` is the ONE place every mutating endpoint required by
`docs/rbac.md` §5 (`*.approve`, `*.override`, `user.manage`, `role.assign`,
`stock.adjust`, `asset.retire`, checkout, reservation changes) calls to
record a before/after entry, in the same DB transaction as the mutation
itself (call it inside the view/service function that already holds the
`ATOMIC_REQUESTS` transaction, before returning the response).
"""

from __future__ import annotations

from typing import Any, Optional

from .models import AuditLog


def write_audit_log(
    *,
    tenant_id: int,
    actor,
    action: str,
    entity_type: str,
    entity_id: Any,
    before: Optional[dict] = None,
    after: Optional[dict] = None,
    ip: Optional[str] = None,
) -> AuditLog:
    """Create one immutable `AuditLog` row.

    Uses `.all_objects.create(...)` (not the tenant-scoped `.objects`
    manager) because the caller already has `tenant_id` in hand explicitly
    (from the authenticated request/session, never client input) and this
    is a pure write, not a tenant-scoped read — mirrors the same, deliberate
    escape-hatch pattern used by `apps.rbac.signals`/the T0.6 seed command.
    """
    return AuditLog.all_objects.create(
        tenant_id=tenant_id,
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        before=before,
        after=after,
        ip=ip,
    )


def client_ip(request) -> Optional[str]:
    """Best-effort client IP for the audit entry.

    Trusts `X-Forwarded-For`'s first hop only behind our own nginx (same
    trust boundary as `SECURE_PROXY_SSL_HEADER` in `config/settings/base.py`);
    falls back to `REMOTE_ADDR`.
    """
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")
