"""Reservation endpoints (T3.2): `docs/api-and-ui.md` "Reservations &
checkout" table.

- `GET /api/v1/reservations?from&to` -- paginated list / calendar feed,
  scope-aware (a caller sees reservations for assets in their viewable
  project scope, same rule as `apps.assets.api.AssetViewSet`).
- `GET /api/v1/reservations/{id}` -- detail.
- `POST /api/v1/reservations` -- create; delegates the conflict/limit/
  approval-routing logic to `apps.reservations.services.create_reservation`.
- `POST /api/v1/reservations/{id}/approve` -- `reservation.approve`, scoped.
- `POST /api/v1/reservations/{id}/reject` -- `reservation.approve`, scoped.
- `POST /api/v1/reservations/{id}/cancel` -- the requester, or a scoped
  approver.

Tenant scoping (golden-path step 2): every `get_queryset()` below is built
from the tenant-scoped `.objects` manager, per-request, never a class-level
`queryset = ...` (same reasoning as `apps.stock.api`/`apps.assets.api`) -- a
reservation id from another tenant simply never matches, i.e. 404 via DRF's
own `get_object()`.

Every approve/reject/cancel writes an `AuditLog` entry
(`CLAUDE.md`: "... reservation change writes an immutable AuditLog entry").
Create is audited too (`reservation.create` is a mutating, tenant-scoped
booking action) -- see `_AUDITED_ACTIONS` below for the exact action-key
used per endpoint.
"""

from __future__ import annotations

from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.filters import OrderingFilter
from rest_framework.response import Response

from apps.audit.services import client_ip, write_audit_log
from apps.common.pagination import BoundedPageNumberPagination
from apps.dashboard.cache import invalidate_tenant_dashboard
from apps.rbac.permission_keys import ASSET_VIEW, RESERVATION_APPROVE, RESERVATION_CREATE
from apps.rbac.services import get_viewable_project_scope

from .models import Reservation
from .permissions import ReservationPermission
from .serializers import ReservationSerializer
from .services import (
    approve_reservation,
    cancel_reservation,
    create_reservation,
    reject_reservation,
)


def _reservation_snapshot(reservation: Reservation) -> dict:
    """Before/after JSONB shape for the audit entries below -- the fields an
    approve/reject/cancel actually changes."""
    return {
        "status": reservation.status,
        "approver_id": reservation.approver_id,
        "approval_note": reservation.approval_note,
    }


class ReservationViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = ReservationSerializer
    permission_classes = [ReservationPermission]
    filter_backends = [DjangoFilterBackend, OrderingFilter]
    filterset_fields = ["status", "asset", "user"]
    ordering_fields = ["start_at", "end_at", "created_at"]
    pagination_class = BoundedPageNumberPagination

    def get_queryset(self):
        # Tenant-scoped manager, resolved per-request (see module docstring).
        qs = Reservation.objects.select_related(
            "asset", "asset__project", "user", "project", "approver"
        ).order_by("-start_at")

        if self.action == "list":
            # Scope-aware listing (docs/rbac.md §1), same pattern as
            # `apps.stock.api.StockItemViewSet.get_queryset` -- a caller
            # whose ONLY `asset.view` grant(s) are project-scoped sees only
            # reservations for THOSE projects' assets, never another
            # project's or the general pool.
            tenant_wide, project_ids = get_viewable_project_scope(self.request.user, ASSET_VIEW)
            if not tenant_wide:
                qs = qs.filter(asset__project_id__in=project_ids) if project_ids else qs.none()

            # Calendar feed: `?from=&to=` (ISO-8601 datetimes) restricts to
            # reservations whose `[start_at, end_at)` window overlaps
            # `[from, to)` -- same overlap test as the F4 conflict check.
            # Either bound may be supplied alone (open-ended on that side).
            from_param = self.request.query_params.get("from")
            to_param = self.request.query_params.get("to")
            if to_param:
                qs = qs.filter(start_at__lt=to_param)
            if from_param:
                qs = qs.filter(end_at__gt=from_param)

        return qs

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        validated = serializer.validated_data

        reservation = create_reservation(
            tenant=request.user.tenant,
            actor=request.user,
            asset=validated["asset"],
            start_at=validated["start_at"],
            end_at=validated["end_at"],
            project=validated.get("project"),
        )

        write_audit_log(
            tenant_id=reservation.tenant_id,
            actor=request.user,
            action=RESERVATION_CREATE,
            entity_type="reservation",
            entity_id=reservation.id,
            before=None,
            after=_reservation_snapshot(reservation),
            ip=client_ip(request),
        )
        # T5.5: a new reservation may fall inside the "upcoming reservations"
        # window -- invalidate so the dashboard reflects it without waiting
        # out the TTL (see `apps.dashboard.cache` module docstring).
        invalidate_tenant_dashboard(reservation.tenant_id)

        out = self.get_serializer(reservation)
        return Response(out.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        reservation = self.get_object()  # tenant + scoped RBAC already enforced
        before = _reservation_snapshot(reservation)
        note = request.data.get("approval_note", "") or request.data.get("note", "")

        updated = approve_reservation(reservation=reservation, actor=request.user, note=note)

        write_audit_log(
            tenant_id=updated.tenant_id,
            actor=request.user,
            action=RESERVATION_APPROVE,
            entity_type="reservation",
            entity_id=updated.id,
            before=before,
            after=_reservation_snapshot(updated),
            ip=client_ip(request),
        )
        invalidate_tenant_dashboard(updated.tenant_id)

        return Response(self.get_serializer(updated).data)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        reservation = self.get_object()
        before = _reservation_snapshot(reservation)
        note = request.data.get("approval_note", "") or request.data.get("note", "")

        updated = reject_reservation(reservation=reservation, actor=request.user, note=note)

        write_audit_log(
            tenant_id=updated.tenant_id,
            actor=request.user,
            # Rejecting is the SAME permission (`reservation.approve`)
            # exercised as approving -- both are the approver's decision on
            # a pending request (mirrors `apps.stock.api`'s reorder-approval
            # audit-action-key choice for its APPROVER_ONLY_TRANSITIONS).
            action=RESERVATION_APPROVE,
            entity_type="reservation",
            entity_id=updated.id,
            before=before,
            after=_reservation_snapshot(updated),
            ip=client_ip(request),
        )
        invalidate_tenant_dashboard(updated.tenant_id)

        return Response(self.get_serializer(updated).data)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        reservation = self.get_object()
        before = _reservation_snapshot(reservation)

        updated = cancel_reservation(reservation=reservation, actor=request.user)

        write_audit_log(
            tenant_id=updated.tenant_id,
            actor=request.user,
            action="reservation.cancel",
            entity_type="reservation",
            entity_id=updated.id,
            before=before,
            after=_reservation_snapshot(updated),
            ip=client_ip(request),
        )
        invalidate_tenant_dashboard(updated.tenant_id)

        return Response(self.get_serializer(updated).data)
