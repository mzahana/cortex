"""Stock endpoints (T2.2): `docs/api-and-ui.md` "Stock" table.

- `GET /api/v1/stock` — paginated list, `?low_stock=true` filter, scope-aware
  (a caller sees stock for assets in their viewable project scope, same rule
  as `apps.assets.api.AssetViewSet`).
- `GET /api/v1/stock/{id}` — detail.
- `POST /api/v1/stock/{id}/txn` — apply a ledger transaction
  (receive/consume/adjust/correction). The core of this task: delegates the
  atomic, concurrency-safe ledger-apply + quantity reconciliation to
  `apps.stock.services.apply_stock_txn`; audits `stock.adjust`-reason txns
  (`docs/rbac.md` §5).
- `GET/POST/PATCH /api/v1/reorder-requests` (`/{id}`) — the reorder workflow;
  `PATCH` drives status transitions (`apps.stock.services.
  apply_reorder_transition`), auditing the `approved` transition.

Tenant scoping (golden-path step 2): every `get_queryset()` below is built
from the tenant-scoped `.objects` manager, per-request, never a class-level
`queryset = ...` (same reasoning as `apps.assets.api`/`apps.catalog.api`) — a
stock/reorder id from another tenant simply never matches, i.e. 404 via
`get_object_or_404`/DRF's own `get_object()`.
"""

from __future__ import annotations

from django.db.models import F
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.filters import OrderingFilter
from rest_framework.response import Response

from apps.audit.services import client_ip, write_audit_log
from apps.common.pagination import BoundedPageNumberPagination
from apps.dashboard.cache import invalidate_tenant_dashboard
from apps.rbac.permission_keys import ASSET_VIEW, REORDER_APPROVE, STOCK_ADJUST
from apps.rbac.services import get_viewable_project_scope

from .models import ReorderRequest, StockItem
from .permissions import APPROVER_ONLY_TRANSITIONS, ReorderRequestPermission, StockItemPermission
from .serializers import ReorderRequestSerializer, StockItemSerializer, StockTxnSerializer
from .services import apply_reorder_transition, apply_stock_txn

# `stock.adjust` reasons per docs/rbac.md §5 ("every `stock.adjust` ...
# writes an AuditLog entry") — `consume` is deliberately excluded (not in
# rbac.md §5's mandatory-audit list).
_AUDITED_TXN_REASONS = {"receive", "adjust", "correction"}


class StockItemViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = StockItemSerializer
    permission_classes = [StockItemPermission]
    filter_backends = [DjangoFilterBackend, OrderingFilter]
    ordering_fields = ["quantity_on_hand", "reorder_threshold", "created_at"]
    pagination_class = BoundedPageNumberPagination

    def get_queryset(self):
        # Tenant-scoped manager, resolved per-request (see module docstring).
        qs = StockItem.objects.select_related("asset", "asset__project", "bin_location").order_by(
            "-created_at"
        )

        low_stock = self.request.query_params.get("low_stock", "").lower() == "true"
        if low_stock:
            # `stock_item (tenant_id)` partial index WHERE quantity_on_hand
            # <= reorder_threshold (T2.3, docs/data-model.md §3) is built
            # for exactly this comparison.
            qs = qs.filter(quantity_on_hand__lte=F("reorder_threshold"))

        if self.action == "list":
            # Scope-aware listing (docs/rbac.md §1), same pattern as
            # `apps.assets.api.AssetViewSet.get_queryset` — a caller whose
            # ONLY `asset.view` grant(s) are project-scoped sees only stock
            # for THOSE projects' assets, never another project's or the
            # general pool.
            tenant_wide, project_ids = get_viewable_project_scope(self.request.user, ASSET_VIEW)
            if not tenant_wide:
                qs = qs.filter(asset__project_id__in=project_ids) if project_ids else qs.none()
        return qs

    @action(detail=True, methods=["post"])
    def txn(self, request, pk=None):
        stock_item = self.get_object()  # tenant + reason-scoped RBAC already enforced

        # `stock_item` comes from the URL (`{id}`), never trusted from the
        # body — inject it before validation so `StockTxnSerializer`'s
        # tenant-scoped `stock_item` field always resolves to the SAME
        # already-RBAC-checked object, regardless of what (if anything) the
        # client sent in the body for that key.
        data = request.data.copy() if hasattr(request.data, "copy") else dict(request.data)
        data["stock_item"] = stock_item.id
        serializer = StockTxnSerializer(data=data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        delta = serializer.validated_data["delta"]
        reason = serializer.validated_data["reason"]
        ref = serializer.validated_data.get("ref", "")

        result = apply_stock_txn(
            stock_item=stock_item,
            delta=delta,
            reason=reason,
            ref=ref,
            actor=request.user,
        )

        if reason in _AUDITED_TXN_REASONS:
            write_audit_log(
                tenant_id=result.stock_item.tenant_id,
                actor=request.user,
                action=STOCK_ADJUST,
                entity_type="stock_item",
                entity_id=result.stock_item.id,
                before={"quantity_on_hand": result.previous_quantity},
                after={
                    "quantity_on_hand": result.new_quantity,
                    "txn_id": result.txn.id,
                    "reason": reason,
                    "delta": delta,
                },
                ip=client_ip(request),
            )
            # T5.5: an audited txn changes `quantity_on_hand`, which the
            # `low_stock` tile aggregates over -- invalidate immediately
            # (see `apps.dashboard.cache` module docstring).
            invalidate_tenant_dashboard(result.stock_item.tenant_id)

        return Response(
            {
                "stock_item": StockItemSerializer(result.stock_item).data,
                "txn": StockTxnSerializer(result.txn).data,
                "low_stock": result.new_quantity <= result.stock_item.reorder_threshold,
            },
            status=status.HTTP_201_CREATED,
        )


class ReorderRequestViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = ReorderRequestSerializer
    permission_classes = [ReorderRequestPermission]
    http_method_names = ["get", "post", "patch", "head", "options"]
    filter_backends = [DjangoFilterBackend, OrderingFilter]
    filterset_fields = ["status"]
    ordering_fields = ["requested_at", "decided_at", "created_at"]
    pagination_class = BoundedPageNumberPagination

    def get_queryset(self):
        qs = ReorderRequest.objects.select_related(
            "stock_item", "stock_item__asset", "requested_by", "approved_by"
        ).order_by("-created_at")

        if self.action == "list":
            tenant_wide, project_ids = get_viewable_project_scope(self.request.user, ASSET_VIEW)
            if not tenant_wide:
                qs = (
                    qs.filter(stock_item__asset__project_id__in=project_ids)
                    if project_ids
                    else qs.none()
                )
        return qs

    def perform_update(self, serializer):
        new_status = serializer.validated_data.get("status")
        instance: ReorderRequest = serializer.instance

        if new_status is None or new_status == instance.status:
            # A plain field edit (quantity/note) or a no-op resend of the
            # current status — no transition service call needed.
            serializer.save()
            return

        before = {
            "status": instance.status,
            "approved_by_id": instance.approved_by_id,
            "decided_at": instance.decided_at.isoformat() if instance.decided_at else None,
        }
        updated = apply_reorder_transition(
            reorder_request=instance,
            new_status=new_status,
            actor=self.request.user,
        )
        # Keep DRF's own post-`perform_update` response building in sync
        # with what the service actually persisted.
        serializer.instance = updated

        if new_status in APPROVER_ONLY_TRANSITIONS:
            # `docs/rbac.md` §5: every exercise of `reorder.approve` writes
            # an AuditLog entry — NOT just the `open -> approved` decision.
            # `approved -> ordered` and `ordered -> received` are ALSO
            # gated by `reorder.approve` (`apps.stock.permissions.
            # APPROVER_ONLY_TRANSITIONS`), so all three are audited here;
            # `cancelled` is deliberately excluded (a self-cancel by the
            # original requester is not an exercise of `reorder.approve` —
            # see that permission class's module docstring).
            after = {
                "status": updated.status,
                "approved_by_id": updated.approved_by_id,
                "decided_at": updated.decided_at.isoformat() if updated.decided_at else None,
            }
            write_audit_log(
                tenant_id=updated.tenant_id,
                actor=self.request.user,
                action=REORDER_APPROVE,
                entity_type="reorder_request",
                entity_id=updated.id,
                before=before,
                after=after,
                ip=client_ip(self.request),
            )
