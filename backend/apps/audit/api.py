"""`GET /api/v1/audit` (T5.3, `docs/api-and-ui.md`: "Audit log (scoped)").

Read-only (no create/update/destroy — entries are ONLY ever written via
`apps.audit.services.write_audit_log` from the mutation sites themselves,
never through this API; see `docs/data-model.md` §2: "append-only,
immutable... No update/delete permitted"). T5.4 (db-migration-specialist)
layers the DB-level trigger backstop on top of this app-level guard (no
update/destroy route exists here at all).

Tenant scoping (golden-path step 2): `get_queryset()` builds
`AuditLog.objects...` (tenant-scoped, fail-closed manager) fresh per
request, never a class-level `queryset = ...` — same reasoning as
`apps.assets.api`/`apps.catalog.api`.

RBAC + scoping (golden-path step 3, docs/rbac.md §3 footnote 4): an Admin
(tenant-wide `audit.view` grant) sees every entry in the tenant; a
ProjectLead (project-scoped grant only) sees ONLY entries for their own
project's assets. `AuditLog` has no `project_id` column of its own
(`docs/data-model.md` §2's schema doesn't carry one — flagged below for
T5.4 to consider a denormalized `project_id` + index if this scoped filter
ever shows up in profiling), so the ProjectLead-scoped filter is built here
by resolving each known `entity_type` back to its owning `Asset`'s
`project_id` — a small, BOUNDED number of extra queries (one per known
entity_type, never per audit-log row), same query-budget discipline as
every other scoped list endpoint in this codebase.
"""

from __future__ import annotations

import django_filters as filters
from django.db.models import Q
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import mixins, viewsets
from rest_framework.filters import OrderingFilter

from apps.assets.models import Asset
from apps.common.pagination import BoundedPageNumberPagination
from apps.rbac.models import Membership
from apps.rbac.permission_keys import AUDIT_VIEW
from apps.rbac.services import get_viewable_project_scope
from apps.reservations.models import Checkout, Reservation
from apps.stock.models import ReorderRequest, StockItem

from .models import AuditLog
from .permissions import AuditLogPermission
from .serializers import AuditLogSerializer


class AuditLogFilterSet(filters.FilterSet):
    """`?entity_type=`/`?entity_id=`/`?action=`/`?actor=`/`?created_after=`/
    `?created_before=` (docs/api-and-ui.md: "Filterable immutable history").
    Plain equality/range filters — `actor` is a bare id-equality
    `NumberFilter`, deliberately not an auto-generated `ModelChoiceFilter`
    (same reasoning as `apps.rbac.api.MembershipFilterSet`)."""

    entity_type = filters.CharFilter(field_name="entity_type")
    entity_id = filters.CharFilter(field_name="entity_id")
    action = filters.CharFilter(field_name="action")
    actor = filters.NumberFilter(field_name="actor_id")
    created_after = filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="gte")
    created_before = filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="lte")

    class Meta:
        model = AuditLog
        fields = ["entity_type", "entity_id", "action", "actor", "created_after", "created_before"]


def _project_scoped_entity_filter(project_ids: set[int]) -> Q:
    """`Q` restricting `AuditLog` rows to entries whose entity belongs to one
    of `project_ids` — one bounded lookup per known `entity_type` (docs/
    rbac.md §5's mandatory-audit action list: `asset.retire` -> `asset`,
    `stock.adjust` -> `stock_item`, `reservation.*` -> `reservation`,
    `checkout.*` -> `checkout`, `reorder.approve` -> `reorder_request`,
    `user.manage`/`role.assign` -> `membership`, itself already carrying its
    own `project_id`, not via an `Asset`).
    """

    def _ids(qs) -> list[str]:
        return [str(pk) for pk in qs.values_list("pk", flat=True)]

    asset_ids = _ids(Asset.objects.filter(project_id__in=project_ids))
    stock_item_ids = _ids(StockItem.objects.filter(asset__project_id__in=project_ids))
    reservation_ids = _ids(Reservation.objects.filter(asset__project_id__in=project_ids))
    checkout_ids = _ids(Checkout.objects.filter(asset__project_id__in=project_ids))
    reorder_request_ids = _ids(
        ReorderRequest.objects.filter(stock_item__asset__project_id__in=project_ids)
    )
    membership_ids = _ids(Membership.objects.filter(project_id__in=project_ids))

    return (
        Q(entity_type="asset", entity_id__in=asset_ids)
        | Q(entity_type="stock_item", entity_id__in=stock_item_ids)
        | Q(entity_type="reservation", entity_id__in=reservation_ids)
        | Q(entity_type="checkout", entity_id__in=checkout_ids)
        | Q(entity_type="reorder_request", entity_id__in=reorder_request_ids)
        | Q(entity_type="membership", entity_id__in=membership_ids)
    )


class AuditLogViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    serializer_class = AuditLogSerializer
    permission_classes = [AuditLogPermission]
    filter_backends = [DjangoFilterBackend, OrderingFilter]
    filterset_class = AuditLogFilterSet
    ordering_fields = ["created_at"]
    pagination_class = BoundedPageNumberPagination

    def get_queryset(self):
        # Tenant-scoped manager, resolved per-request (see module docstring).
        qs = AuditLog.objects.select_related("actor").order_by("-created_at")

        tenant_wide, project_ids = get_viewable_project_scope(self.request.user, AUDIT_VIEW)
        if tenant_wide:
            return qs
        if not project_ids:
            return qs.none()
        return qs.filter(_project_scoped_entity_filter(project_ids))
