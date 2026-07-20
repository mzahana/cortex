"""Redis caching for `GET /api/v1/dashboard/summary` (T5.5, `docs/features.md`
F10, `docs/api-and-ui.md`: "Aggregates (cached)").

Reuses the SAME Redis-backed `django.core.cache` `"default"` alias every
other part of this codebase already uses for caching (`config/settings/
base.py`'s `CACHES["default"]` -> `django_redis`, also the session backend)
-- `apps.notifications.tasks`'s `cache.add(...)` low-stock/overdue-reminder
throttle guard (T5.2) is the precedent this deliberately matches: no new
caching library, no new Redis connection, just the plain Django cache API.

## Strategy: short TTL + a per-tenant version counter (not per-mutation key
   deletion)

The task explicitly scopes this down from a full pub/sub invalidation bus to
something proportionate for an MVP dashboard:

  1. **Short TTL is the primary staleness bound** (`DASHBOARD_CACHE_TTL_SECONDS`,
     30s) -- even with NO invalidation wired up at all, no tile is ever stale
     for longer than this.
  2. **Best-effort invalidation on the highest-value mutations** (checkout /
     check-in / override-return, reservation create/approve/reject/cancel,
     an audited stock txn, asset retire) tightens that bound to "immediately"
     for the tiles that move the most visibly (`currently_out`/`overdue`
     especially -- CLAUDE.md's own example).

The cache key is per-tenant AND per RBAC-scope (`docs/rbac.md` §1: a
ProjectLead's dashboard must never leak the tenant-wide numbers, and two
different ProjectLeads scoped to different projects must never share a
cached response) -- see `summary_cache_key` below. Rather than trying to
enumerate and delete every scope-specific key a mutation might affect (which
would require re-deriving the exact set of viewer scopes touched by each
write -- expensive and easy to get subtly wrong), invalidation bumps ONE
per-tenant version counter; every scope's cache key embeds that version, so
bumping it invalidates every scope's cached response for that tenant at
once with a single O(1) Redis write. The (harmless) trade-off: a mutation in
one project also forces a cache miss for another project's dashboard in the
same tenant until the next request repopulates it -- acceptable for MVP
scale (a lab's tenant), and still bounded by the same short TTL either way.
"""

from __future__ import annotations

from django.core.cache import cache

# 30s: short enough that "cache reflects live state" (the T5.5 exit
# criterion) is true well within a page refresh, long enough to make the
# cache worth having under normal dashboard-polling load.
DASHBOARD_CACHE_TTL_SECONDS = 30

_CACHE_KEY_PREFIX = "dashboard:summary"
_VERSION_KEY_PREFIX = "dashboard:version"


def _version_key(tenant_id: int) -> str:
    return f"{_VERSION_KEY_PREFIX}:{tenant_id}"


def _get_tenant_version(tenant_id: int) -> int:
    """The current cache-busting version for `tenant_id`, initializing it to
    `1` (no expiry -- this counter is small, cheap, and must outlive any
    individual summary's TTL) the first time it's read for a tenant."""
    version = cache.get(_version_key(tenant_id))
    if version is None:
        version = 1
        cache.set(_version_key(tenant_id), version, timeout=None)
    return version


def invalidate_tenant_dashboard(tenant_id: int) -> None:
    """Bump `tenant_id`'s dashboard cache version, invalidating every
    scope's cached summary for that tenant at once (see module docstring).
    Called from the highest-value mutation sites (checkout/checkin,
    reservation approve/reject/cancel/create, an audited stock txn, asset
    retire) -- best-effort; the short TTL is the real staleness backstop, so
    a call site missed here is not a correctness bug, only a slightly
    staler-than-necessary tile for up to `DASHBOARD_CACHE_TTL_SECONDS`.
    """
    key = _version_key(tenant_id)
    try:
        cache.incr(key)
    except ValueError:
        # Nothing cached yet for this tenant (first mutation ever, or the
        # version key expired/was evicted) -- seed a version guaranteed to
        # differ from the default `1` any not-yet-invalidated cached entry
        # was built with, so this still counts as an invalidation.
        cache.set(key, 2, timeout=None)


def summary_cache_key(tenant_id: int, *, tenant_wide: bool, project_ids: frozenset[int]) -> str:
    """The cache key for one tenant + one RBAC scope (docs/rbac.md §1
    union-of-memberships rule, `apps.rbac.services.get_viewable_project_scope`'s
    `(tenant_wide, project_ids)` pair) -- two different scopes for the same
    tenant NEVER share a cache entry, so a ProjectLead can never be served a
    cached response computed for a different (wider or differently-scoped)
    viewer.
    """
    version = _get_tenant_version(tenant_id)
    if tenant_wide:
        scope = "all"
    elif project_ids:
        scope = "p:" + ",".join(str(pid) for pid in sorted(project_ids))
    else:
        scope = "none"
    return f"{_CACHE_KEY_PREFIX}:{tenant_id}:{version}:{scope}"
