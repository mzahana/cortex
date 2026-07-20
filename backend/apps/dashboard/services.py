"""Aggregation for `GET /api/v1/dashboard/summary` (T5.5, `docs/features.md`
F10: "dashboard: totals by category, currently-out, overdue, low-stock,
upcoming reservations, per-project allocation").

Read-only — no new tenant-owned tables (pure aggregation over `Asset`,
`Checkout`, `Reservation`, `StockItem`, all already tenant-scoped/RLS-backed
by their own apps). Called from a request already inside the per-request
`tenant_context()` (`apps.tenancy.middleware.CurrentTenantMiddleware`), so
every queryset below goes through the normal fail-closed `.objects` manager
-- same golden-path rule as every other app, no new escape hatch needed here.

## RBAC scoping (docs/rbac.md §1, CLAUDE.md invariant)

Gated on `asset.view` -- the same permission key/scope-resolution helper
(`apps.rbac.services.get_viewable_project_scope`) `apps.assets.api.
AssetViewSet`/`apps.stock.api.StockItemViewSet`/`apps.reservations.api.
ReservationViewSet`/`apps.reservations.checkout.CheckoutViewSet` already use
for their own scoped LIST endpoints -- resolved exactly ONCE per call and
reused across every tile's query (one membership lookup, not one per tile),
so an Admin's tenant-wide grant sees every asset/checkout/reservation/stock
row in the tenant, while a pure ProjectLead (project-scoped `asset.view`
only) sees ONLY their project(s)' rows in every tile -- never another
project's, never the general pool (docs/rbac.md §1: general-pool rows are
governed by tenant-wide grants only).

## Query budget

Exactly ONE membership-resolving query (`get_viewable_project_scope`) plus
SIX aggregate queries (category totals, per-project totals, currently-out
count, overdue count, low-stock count, upcoming-reservations count) --
`Count`/conditional-filter aggregates, never a per-row Python loop, so the
query count is constant regardless of corpus size (CLAUDE.md: "kill N+1s...
assert query budgets"; proved at 10k-asset scale by
`apps.dashboard.tests.test_dashboard_summary`).

## ASSUMPTION (flagged, task explicitly allows picking a defensible default):
"upcoming reservations" window

Neither `docs/features.md` F10 nor `docs/api-and-ui.md` specifies the exact
window. This uses **the next 7 days** (`UPCOMING_RESERVATION_WINDOW_DAYS`),
counting `approved` reservations (the ACTIVE, confirmed-to-happen status --
`pending` ones may never be approved at all, so counting them here would
overstate what's actually about to happen) whose `start_at` falls in
`[now, now + 7 days)`. The window length is exposed in the response
(`upcoming_reservations_window_days`) so a future UI/spec change can surface
or adjust it without an API contract break.
"""

from __future__ import annotations

from datetime import timedelta

from django.core.cache import cache
from django.db.models import Count, F, Q
from django.utils import timezone

from apps.assets.models import Asset
from apps.rbac.permission_keys import ASSET_VIEW
from apps.rbac.services import get_viewable_project_scope
from apps.reservations.models import Checkout, Reservation
from apps.stock.models import StockItem

from .cache import DASHBOARD_CACHE_TTL_SECONDS, summary_cache_key

UPCOMING_RESERVATION_WINDOW_DAYS = 7


def _scope_filter(field_name: str, *, tenant_wide: bool, project_ids: frozenset[int]) -> Q:
    """`Q` restricting a queryset to `project_ids` via `field_name` (e.g.
    `"project_id"` for `Asset`, `"asset__project_id"` for
    `Checkout`/`Reservation`/`StockItem`) -- `Q()` (no-op) for a tenant-wide
    viewer, `Q(pk__in=[])` (matches nothing) for a viewer with no viewable
    project at all, same three-way shape `apps.assets.api.AssetViewSet.
    get_queryset` and friends already use inline.
    """
    if tenant_wide:
        return Q()
    if not project_ids:
        return Q(pk__in=[])
    return Q(**{f"{field_name}__in": project_ids})


def _build_summary(*, tenant_wide: bool, project_ids: frozenset[int]) -> dict:
    asset_scope = _scope_filter("project_id", tenant_wide=tenant_wide, project_ids=project_ids)
    related_scope = _scope_filter(
        "asset__project_id", tenant_wide=tenant_wide, project_ids=project_ids
    )

    active_assets = Asset.objects.exclude(status=Asset.Status.RETIRED).filter(asset_scope)

    totals_by_category = [
        {
            "category_id": row["category_id"],
            "category_name": row["category__name"],
            "count": row["count"],
        }
        for row in (
            active_assets.values("category_id", "category__name")
            .annotate(count=Count("id"))
            .order_by("category__name")
        )
    ]

    per_project_allocation = [
        {
            "project_id": row["project_id"],
            "project_name": row["project__name"] or "General pool",
            "count": row["count"],
        }
        for row in (
            active_assets.values("project_id", "project__name")
            .annotate(count=Count("id"))
            .order_by("project__name")
        )
    ]

    currently_out = (
        Checkout.objects.filter(checked_in_at__isnull=True).filter(related_scope).count()
    )

    now = timezone.now()
    overdue = (
        Checkout.objects.filter(checked_in_at__isnull=True, due_at__lt=now)
        .filter(related_scope)
        .count()
    )

    low_stock = (
        StockItem.objects.filter(quantity_on_hand__lte=F("reorder_threshold"))
        .filter(related_scope)
        .count()
    )

    window_end = now + timedelta(days=UPCOMING_RESERVATION_WINDOW_DAYS)
    upcoming_reservations = (
        Reservation.objects.filter(
            status=Reservation.Status.APPROVED,
            start_at__gte=now,
            start_at__lt=window_end,
        )
        .filter(related_scope)
        .count()
    )

    return {
        "totals_by_category": totals_by_category,
        "currently_out": currently_out,
        "overdue": overdue,
        "low_stock": low_stock,
        "upcoming_reservations": upcoming_reservations,
        "upcoming_reservations_window_days": UPCOMING_RESERVATION_WINDOW_DAYS,
        "per_project_allocation": per_project_allocation,
        "generated_at": now.isoformat(),
    }


def get_dashboard_summary(user) -> dict:
    """The (possibly cached) dashboard summary for `user`, scoped per
    docs/rbac.md §1. See the cache-strategy docstring in `.cache` for the
    TTL + invalidation design.
    """
    tenant_wide, project_ids = get_viewable_project_scope(user, ASSET_VIEW)
    project_ids = frozenset(project_ids)

    cache_key = summary_cache_key(user.tenant_id, tenant_wide=tenant_wide, project_ids=project_ids)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    summary = _build_summary(tenant_wide=tenant_wide, project_ids=project_ids)
    cache.set(cache_key, summary, timeout=DASHBOARD_CACHE_TTL_SECONDS)
    return summary
