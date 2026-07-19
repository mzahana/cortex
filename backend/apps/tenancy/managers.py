"""Central tenant-scoped queryset/manager (T0.4).

Every tenant-owned model in the codebase gets its default manager from here
via `TenantScopedModel` (see `models.py`). This is the "base manager +
middleware" pattern required by docs/architecture.md §3 and CLAUDE.md:
tenant isolation is enforced centrally, once, not re-implemented per view.

Postgres RLS (T0.5) is defense-in-depth on top of this — never a substitute.
"""

from __future__ import annotations

from django.db import models

from .context import TenantContextError, get_current_tenant_id


def _require_current_tenant_id() -> int:
    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        raise TenantContextError(
            "Refusing to run a tenant-scoped query with no tenant in "
            "context (fail-closed). The current tenant must come from "
            "the authenticated session via CurrentTenantMiddleware, or "
            "be set explicitly with "
            "`apps.tenancy.context.tenant_context(tenant_id)` in "
            "management commands / Celery tasks / tests."
        )
    return tenant_id


class TenantScopedQuerySet(models.QuerySet):
    def for_current_tenant(self):
        """Re-apply the current-tenant filter explicitly (rarely needed;
        `.get_queryset()` on the manager already does this on every access)."""
        return self.filter(tenant_id=_require_current_tenant_id())


class TenantScopedManager(models.Manager.from_queryset(TenantScopedQuerySet)):  # type: ignore[misc]
    """The default manager (`.objects`) on every `TenantScopedModel`.

    Always filters by the tenant in the current context. Raises
    `TenantContextError` if no tenant is set — fail-closed, never fail-open.
    """

    use_in_migrations = True

    def get_queryset(self) -> TenantScopedQuerySet:
        qs = super().get_queryset()
        return qs.filter(tenant_id=_require_current_tenant_id())
