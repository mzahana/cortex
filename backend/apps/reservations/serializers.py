"""Serializers for the reservation endpoints (T3.2).

Tenant scoping (same golden-path rule as `apps.stock.serializers`/
`apps.assets.serializers`): every writable relation that points at another
tenant-owned model (`Reservation.asset`, `Reservation.project`) gets its
queryset built in `get_fields()`, from that model's tenant-scoped `.objects`
manager, resolved lazily per-request -- never a class-body
`queryset=Model.objects.all()`.

Business rules (conflict/limit/approval) live in `apps.reservations.services`,
not here (CLAUDE.md: "keep serializers thin; put business rules in
services/model methods, not views") -- this serializer only validates
shape/field-level constraints that would otherwise surface as a raw model
`ValidationError`/`IntegrityError`.
"""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from apps.assets.models import Asset
from apps.projects.models import Project

from .models import Reservation


class ReservationSerializer(serializers.ModelSerializer):
    """`user`/`status`/`approver`/`approval_note` are never client-writable on
    CREATE -- `user` is always the authenticated caller (the requester,
    mirroring `ReorderRequest.requested_by`), and `status`/`approver`/
    `approval_note` are only ever set by `apps.reservations.services`
    (auto-approve vs. `pending`, and the approve/reject actions) via
    `apps.reservations.api`, never accepted raw from the client body.
    """

    class Meta:
        model = Reservation
        fields = [
            "id",
            "asset",
            "user",
            "project",
            "start_at",
            "end_at",
            "status",
            "approver",
            "approval_note",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "user",
            "status",
            "approver",
            "approval_note",
            "created_at",
            "updated_at",
        ]

    def get_fields(self):
        fields = super().get_fields()
        # Tenant-scoped, resolved per-request (see module docstring) -- R4/F1.
        fields["asset"].queryset = Asset.objects.all()  # type: ignore[attr-defined]
        fields["project"].queryset = Project.objects.all()  # type: ignore[attr-defined]
        return fields

    def validate_asset(self, asset: Asset) -> Asset:
        # Reservations are for DURABLE assets only (`docs/data-model.md` §2:
        # "Reservation -- durable-asset booking"; consumables get a
        # `StockItem` ledger instead, `apps.stock`) -- a clean 400 rather
        # than a confusing conflict/approval flow against a consumable.
        if asset.is_consumable:
            raise serializers.ValidationError(
                "This asset is a consumable (tracked via stock, not reservations)."
            )
        return asset

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        start_at = attrs.get("start_at")
        end_at = attrs.get("end_at")
        if start_at is not None and end_at is not None and end_at <= start_at:
            # The model's own `CheckConstraint` (`reservation_end_after_start`)
            # would also reject this as an `IntegrityError` -> 409, but a
            # field-shape problem like this is a clean 400, not a conflict.
            raise serializers.ValidationError({"end_at": "end_at must be after start_at."})
        return attrs

    # No `create()` override: `POST /reservations` never calls
    # `serializer.save()` -- the conflict pre-check / per-user cap / window
    # validation / auto-approve-vs-pending decision / event emission is all
    # one atomic unit in `apps.reservations.services.create_reservation`, so
    # `apps.reservations.api.ReservationViewSet.create` calls that directly
    # with this serializer's `validated_data` and re-serializes the result
    # for the response. This serializer is otherwise the read/list
    # representation for every other action.
