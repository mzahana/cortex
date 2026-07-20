"""Serializers for the consumables/stock models (T2.1).

Tenant scoping (same golden-path rule as `apps.assets.serializers`/
`apps.catalog.serializers`): every writable relation that points at another
tenant-owned model (`StockItem.asset`, `StockItem.bin_location`,
`StockTxn.stock_item`, `ReorderRequest.stock_item`) gets its queryset built
in `get_fields()`, from that model's tenant-scoped `.objects` manager,
resolved lazily per-request — never a class-body `queryset=Model.objects.all()`.

Endpoints/viewsets are T2.2's job; these serializers are written so T2.2 can
wire them straight into DRF viewsets with no reshape.
"""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from apps.assets.models import Asset
from apps.catalog.models import Location

from .models import ReorderRequest, StockItem, StockTxn


class StockItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = StockItem
        fields = [
            "id",
            "asset",
            "unit_of_measure",
            "quantity_on_hand",
            "reorder_threshold",
            "reorder_target",
            "bin_location",
            "created_at",
            "updated_at",
        ]
        # `quantity_on_hand` is the derived/reconciled ledger figure — never
        # client-settable (docs/data-model.md §2, this task's own
        # instruction). Maintained only by T2.2's ledger-apply service.
        read_only_fields = ["id", "quantity_on_hand", "created_at", "updated_at"]

    def get_fields(self):
        fields = super().get_fields()
        # Tenant-scoped, resolved per-request (see module docstring) — R4/F1.
        fields["asset"].queryset = Asset.objects.all()  # type: ignore[attr-defined]
        fields["bin_location"].queryset = Location.objects.all()  # type: ignore[attr-defined]
        return fields

    def validate_asset(self, asset: Asset) -> Asset:
        # Clean 400 (not an `IntegrityError` -> 500) for both invariant
        # violations the model also enforces at `save()` time (module
        # docstring in `models.py`): only a consumable asset may get a
        # StockItem, and at most one per asset.
        if not asset.is_consumable:
            raise serializers.ValidationError("Only a consumable asset may own a StockItem.")
        qs = StockItem.objects.filter(asset=asset)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)  # type: ignore[union-attr]
        if qs.exists():
            raise serializers.ValidationError("This asset already has a StockItem.")
        return asset

    def create(self, validated_data: dict[str, Any]) -> StockItem:
        request = self.context["request"]
        validated_data["tenant"] = request.user.tenant
        return StockItem.objects.create(**validated_data)


class StockTxnSerializer(serializers.ModelSerializer):
    """Append-only ledger row (docs/data-model.md §2). `actor` is always the
    authenticated caller, never client-supplied; `delta`/`reason`/`ref` are
    the only client-writable fields, and only on CREATE — see `update()`.
    """

    class Meta:
        model = StockTxn
        fields = [
            "id",
            "stock_item",
            "delta",
            "reason",
            "ref",
            "actor",
            "created_at",
        ]
        read_only_fields = ["id", "actor", "created_at"]

    def get_fields(self):
        fields = super().get_fields()
        fields["stock_item"].queryset = StockItem.objects.all()  # type: ignore[attr-defined]
        return fields

    def validate_delta(self, delta: int) -> int:
        if delta == 0:
            raise serializers.ValidationError("delta must be non-zero.")
        return delta

    def create(self, validated_data: dict[str, Any]) -> StockTxn:
        request = self.context["request"]
        validated_data["tenant"] = request.user.tenant
        validated_data["actor"] = request.user
        return StockTxn.objects.create(**validated_data)

    def update(self, instance: StockTxn, validated_data: dict[str, Any]) -> StockTxn:
        # Append-only (module/model docstring): there is no update path.
        # `StockTxn.save()` would itself raise on an existing pk, but this
        # gives a clean 400 problem+json response instead of surfacing that
        # as an unhandled `ValidationError`/500 from inside `.save()`.
        raise serializers.ValidationError(
            "StockTxn is append-only: an existing ledger row cannot be "
            "edited. Write a new transaction (reason='correction') instead."
        )


class ReorderRequestSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReorderRequest
        fields = [
            "id",
            "stock_item",
            "requested_by",
            "approved_by",
            "quantity",
            "status",
            "note",
            "requested_at",
            "decided_at",
            "created_at",
            "updated_at",
        ]
        # `status` is writable (PATCH transitions, validated below) but
        # `requested_by`/`approved_by`/`decided_at` are only ever set by the
        # view/service layer (T2.2: requester = caller on create,
        # approved_by/decided_at set by the approve/reject action) — never
        # accepted raw from the client.
        read_only_fields = [
            "id",
            "requested_by",
            "approved_by",
            "requested_at",
            "decided_at",
            "created_at",
            "updated_at",
        ]

    def get_fields(self):
        fields = super().get_fields()
        fields["stock_item"].queryset = StockItem.objects.all()  # type: ignore[attr-defined]
        return fields

    def validate_quantity(self, quantity: int) -> int:
        if quantity <= 0:
            raise serializers.ValidationError("quantity must be a positive integer.")
        return quantity

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        new_status = attrs.get("status")
        if self.instance is not None:
            instance: ReorderRequest = self.instance  # type: ignore[assignment]

            if new_status is not None:
                # Single source of truth for the transition rules lives on
                # the model (`ReorderRequest.VALID_TRANSITIONS`) so T2.2's
                # approve/reject service enforces the exact same rule.
                try:
                    instance.validate_transition(new_status)
                except Exception as exc:  # django.core.exceptions.ValidationError
                    raise serializers.ValidationError(
                        getattr(exc, "message_dict", str(exc))
                    ) from exc
            elif instance.status in ReorderRequest.TERMINAL_STATUSES:
                # Code-review follow-up (T2.2): a plain field edit
                # (quantity/note, no `status` in the payload) against a
                # request that is already TERMINAL (`received`/`cancelled`)
                # is rejected outright — a terminal request is immutable,
                # not just "no further status transition".
                raise serializers.ValidationError(
                    {
                        "non_field_errors": (
                            f"This reorder request is '{instance.status}' (terminal) and "
                            "can no longer be edited."
                        )
                    }
                )
        return attrs

    def create(self, validated_data: dict[str, Any]) -> ReorderRequest:
        request = self.context["request"]
        validated_data["tenant"] = request.user.tenant
        validated_data["requested_by"] = request.user
        validated_data.setdefault("status", ReorderRequest.Status.OPEN)
        return ReorderRequest.objects.create(**validated_data)
