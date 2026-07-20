"""Checkout endpoints (T3.3): `docs/api-and-ui.md` "Reservations & checkout"
table's `POST /checkouts`, `POST /checkouts/{id}/checkin`,
`POST /checkouts/{id}/override-return`, `GET /checkouts?open=true&overdue=true`.

Deliberately a single module (serializer + permission + service + viewset)
per this task's own file-layout instruction, to stay entirely clear of
`apps/reservations/services.py`/`api.py`/`serializers.py`, which the parallel
T3.2 task (Reservation create/approve/reject/cancel) owns.

- **Tenant scoping** (golden-path step 2): every queryset below is built
  from the tenant-scoped `.objects` manager, per-request, never a
  class-level `queryset = ...` — same rule as `apps.assets.api`/
  `apps.stock.api`.
- **RBAC** (golden-path step 3): `CheckoutPermission` — the same two-phase
  scoped pattern as `apps.assets.permissions.AssetPermission` /
  `apps.stock.permissions.StockItemPermission` (permissive "holds it
  somewhere" gate at `has_permission()`, precise scope check against the
  object's actual `asset.project_id` at `has_object_permission()`).
  `checkout.manage` gates checkout/checkin; `checkout.override` gates
  `override-return` (docs/rbac.md §3, footnote 2 for the per-category
  approval gate on Member checkout).
- **Audit** (golden-path step 5, CLAUDE.md: "checkout... writes an
  immutable AuditLog entry"): checkout create, checkin, and override-return
  each write one `AuditLog` row; a no-op idempotent checkin/override-return
  (already checked in) writes NONE — same "no duplicate audit on a no-op"
  rule as `apps.assets.api.AssetViewSet.retire`.
- **Open/overdue filters**: expressed directly as queryset predicates
  (`checked_in_at__isnull=True`, `due_at__lt=now()`), backed by the
  `reservations_checkout_open_partial` / `..._overdue_partial` partial
  indexes from `0002` — never "fetch all rows and filter in Python"
  (CLAUDE.md N+1/query-budget discipline).
"""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import BasePermission
from rest_framework.response import Response

from apps.assets.models import Asset
from apps.audit.services import client_ip, write_audit_log
from apps.common.pagination import BoundedPageNumberPagination
from apps.dashboard.cache import invalidate_tenant_dashboard
from apps.rbac.permission_keys import CHECKOUT_MANAGE, CHECKOUT_OVERRIDE, RESERVATION_APPROVE
from apps.rbac.services import (
    get_viewable_project_scope,
    user_has_permission,
    user_has_permission_in_any_scope,
)

from .models import Checkout, Reservation

# Reservation statuses a `POST /checkouts` may reference (task instructions:
# "validate the reservation belongs to the requesting user and is in an
# approved/fulfilled state if provided").
RESERVATION_CHECKOUT_STATUSES: frozenset[str] = frozenset(
    {Reservation.Status.APPROVED, Reservation.Status.FULFILLED}
)

# Asset states a durable asset may be checked out FROM. Deliberately
# excludes IN_USE (already out), MAINTENANCE, RETIRED, LOST. RESERVED is
# included: an approved reservation may (future milestone) move the asset to
# `reserved` ahead of the checkout window; a walk-up checkout of an
# available asset is the common case.
CHECKOUT_ELIGIBLE_ASSET_STATUSES: frozenset[str] = frozenset(
    {Asset.Status.AVAILABLE, Asset.Status.RESERVED}
)


# --------------------------------------------------------------------------
# Permission (two-phase scoped pattern, same template as
# apps.assets.permissions.AssetPermission / apps.stock.permissions.*)
# --------------------------------------------------------------------------


def _resolve_target_project_id_for_asset(asset_id) -> tuple[bool, int | None]:
    """Resolve a `create` request's target `Asset.project_id`, re-scoped
    through the tenant-scoped `Asset.objects` manager (R4: never trust a
    client-supplied id blindly). Mirrors
    `apps.stock.permissions._resolve_target_project_id_for_stock_item`.

    Returns `(found, project_id)` — `found=False` means the id didn't
    resolve inside the caller's tenant at all; the caller lets the
    serializer's own tenant-scoped FK field produce the clean 400 instead of
    guessing a scope.
    """
    if asset_id in (None, "", "null"):
        return False, None
    try:
        asset = Asset.objects.get(pk=asset_id)
    except (Asset.DoesNotExist, ValueError, TypeError):
        return False, None
    return True, asset.project_id


class CheckoutPermission(BasePermission):
    """`CheckoutViewSet`: list/retrieve gated by `checkout.manage` OR
    `checkout.override` in any scope (a user who can only override still
    needs to be able to see the list to act on it); `create`/`checkin`
    gated by `checkout.manage`; `override_return` gated by
    `checkout.override`.

    **`checkin` is self-service only** (docs task wording: override-return
    is "force-return by someone other than the holder", implying plain
    check-in is the holder returning their own item, or `checkout.manage`
    staff processing it — either way the SAME person who holds it). If the
    caller is not the checkout's holder, `checkin` is denied and they must
    use `override-return` (`checkout.override`, scoped) instead — this is
    what keeps the two actions distinct rather than `checkout.manage` alone
    letting anyone silently do what `checkout.override` is meant to gate.
    """

    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False

        action_name = getattr(view, "action", "") or ""

        if action_name in ("list", "retrieve"):
            return user_has_permission_in_any_scope(
                user, CHECKOUT_MANAGE
            ) or user_has_permission_in_any_scope(user, CHECKOUT_OVERRIDE)

        if action_name == "create":
            found, project_id = _resolve_target_project_id_for_asset(request.data.get("asset"))
            if not found:
                # Let the serializer's tenant-scoped `asset` field produce
                # the clean 400 for a missing/bogus/cross-tenant asset
                # rather than this permission class guessing at a scope.
                return True
            return user_has_permission(user, CHECKOUT_MANAGE, project=project_id)

        if action_name == "checkin":
            return user_has_permission_in_any_scope(user, CHECKOUT_MANAGE)

        if action_name == "override_return":
            return user_has_permission_in_any_scope(user, CHECKOUT_OVERRIDE)

        return False  # fail-closed: unmapped action

    def has_object_permission(self, request, view, obj: Checkout) -> bool:
        user = request.user
        action_name = getattr(view, "action", "") or ""
        project_id = obj.asset.project_id

        if action_name in ("list", "retrieve"):
            return (
                obj.user_id == user.id
                or user_has_permission(user, CHECKOUT_MANAGE, project=project_id)
                or user_has_permission(user, CHECKOUT_OVERRIDE, project=project_id)
            )

        if action_name == "checkin":
            if obj.user_id != user.id:
                return False  # not the holder: must use override-return
            return user_has_permission(user, CHECKOUT_MANAGE, project=project_id)

        if action_name == "override_return":
            return user_has_permission(user, CHECKOUT_OVERRIDE, project=project_id)

        return False


# --------------------------------------------------------------------------
# Serializers
# --------------------------------------------------------------------------


class CheckoutSerializer(serializers.ModelSerializer):
    """Read/create representation. `user`/`checked_out_at`/`checked_in_at`/
    `checkin_condition` are never client-writable — see `create()`/the
    checkin/override-return actions below, which are the only writers.
    """

    is_open = serializers.BooleanField(read_only=True)
    is_overdue = serializers.BooleanField(read_only=True)

    class Meta:
        model = Checkout
        fields = [
            "id",
            "asset",
            "user",
            "reservation",
            "checked_out_at",
            "due_at",
            "checked_in_at",
            "checkout_condition",
            "checkin_condition",
            "is_open",
            "is_overdue",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "user",
            "checked_out_at",
            "checked_in_at",
            "checkin_condition",
            "created_at",
            "updated_at",
        ]

    def get_fields(self):
        fields = super().get_fields()
        # Tenant-scoped, resolved per-request (R4) — same rule as
        # apps.stock.serializers.
        fields["asset"].queryset = Asset.objects.all()  # type: ignore[attr-defined]
        fields["reservation"].queryset = Reservation.objects.all()  # type: ignore[attr-defined]
        return fields

    def validate_asset(self, asset: Asset) -> Asset:
        if asset.is_consumable:
            raise serializers.ValidationError(
                "Only a durable (non-consumable) asset may be checked out."
            )
        if asset.status not in CHECKOUT_ELIGIBLE_ASSET_STATUSES:
            raise serializers.ValidationError(
                f"Asset is '{asset.status}' and cannot be checked out right now."
            )
        return asset

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        request = self.context["request"]
        user = request.user
        asset: Asset = attrs["asset"]
        due_at = attrs.get("due_at")

        if due_at is not None and due_at <= timezone.now():
            raise serializers.ValidationError({"due_at": "due_at must be in the future."})

        reservation: Reservation | None = attrs.get("reservation")
        if reservation is not None:
            if reservation.user_id != user.id:
                raise serializers.ValidationError(
                    {"reservation": "This reservation does not belong to you."}
                )
            if reservation.asset_id != asset.id:
                raise serializers.ValidationError(
                    {"reservation": "This reservation is for a different asset."}
                )
            if reservation.status not in RESERVATION_CHECKOUT_STATUSES:
                raise serializers.ValidationError(
                    {"reservation": "This reservation is not approved/fulfilled."}
                )

        # docs/rbac.md §4 + footnote 2: a category configured
        # `requires_approval=true` means a Member may not walk up and check
        # out directly — they need an approved/fulfilled reservation (just
        # validated above). Someone who ALSO holds `reservation.approve` in
        # this scope (Admin tenant-wide, ProjectLead for their project) is
        # the trusted approver role itself and may bypass this, same as they
        # may approve their own category's reservations.
        if asset.category.requires_approval and reservation is None:
            can_bypass = user_has_permission(user, RESERVATION_APPROVE, project=asset.project_id)
            if not can_bypass:
                raise serializers.ValidationError(
                    {
                        "reservation": (
                            "This category requires approval: check out from an "
                            "approved reservation instead of a direct walk-up checkout."
                        )
                    }
                )

        return attrs

    def create(self, validated_data: dict[str, Any]) -> Checkout:
        request = self.context["request"]
        user = request.user
        asset: Asset = validated_data["asset"]

        with transaction.atomic():
            # Lock the asset row first so two concurrent checkout requests
            # for the same asset serialize (same reasoning as
            # apps.stock.services.apply_stock_txn locking StockItem first) —
            # the second request re-reads `status` with the first's write
            # already visible instead of racing past the eligibility check.
            locked_asset = Asset.objects.select_for_update().get(pk=asset.pk)
            if locked_asset.status not in CHECKOUT_ELIGIBLE_ASSET_STATUSES:
                raise serializers.ValidationError(
                    {
                        "asset": (
                            f"Asset is '{locked_asset.status}' and cannot be "
                            "checked out right now."
                        )
                    }
                )

            checked_out_at = timezone.now()
            reservation: Reservation | None = validated_data.get("reservation")
            checkout = Checkout.objects.create(
                tenant=locked_asset.tenant,
                asset=locked_asset,
                user=user,
                reservation=reservation,
                checked_out_at=checked_out_at,
                due_at=validated_data["due_at"],
                checkout_condition=validated_data.get("checkout_condition", ""),
            )
            locked_asset.status = Asset.Status.IN_USE
            locked_asset.save(update_fields=["status", "updated_at"])

            if reservation is not None:
                # Code-review finding (T3.3/T3.2 seam): a reservation-backed
                # checkout must transition the reservation to `fulfilled` in
                # the SAME atomic block, so `cancel_reservation`
                # (`apps.reservations.services`) can never race a checkout
                # into cancelling the window out from under a physically
                # checked-out asset (which would free the `0002` GiST
                # exclusion constraint for a second, overlapping booking
                # while the asset is still out). `FULFILLED` stays in
                # `Reservation.ACTIVE_STATUSES` on purpose: the asset really
                # is unavailable for this window until checked back in, so
                # the window must keep blocking new reservations exactly the
                # way an `approved` one does -- only cancellation (not the
                # exclusion constraint) is what changes once fulfilled.
                # Lock the reservation row so a concurrent
                # cancel-in-flight sees this write (or vice versa) rather
                # than racing past each other.
                locked_reservation = Reservation.objects.select_for_update().get(pk=reservation.pk)
                locked_reservation.status = Reservation.Status.FULFILLED
                locked_reservation.save(update_fields=["status", "updated_at"])

        return checkout


class CheckinSerializer(serializers.Serializer):
    """Body for `checkin`/`override-return`: the only client-writable field
    is the returned condition note."""

    checkin_condition = serializers.CharField(required=False, allow_blank=True, default="")


# --------------------------------------------------------------------------
# Service: shared checkin/override-return logic (idempotent)
# --------------------------------------------------------------------------


def perform_checkin(*, checkout: Checkout, checkin_condition: str) -> tuple[Checkout, bool]:
    """Free the asset and close the checkout. Idempotent: calling this on an
    already-checked-in `Checkout` is a no-op (returns the existing row
    unchanged) rather than erroring or double-freeing/double-auditing — the
    caller (the view) uses the returned `already_checked_in` flag to skip
    writing a duplicate AuditLog entry, same "no-op, no duplicate audit"
    rule as `apps.assets.api.AssetViewSet.retire`.

    **Concurrency:** locks the `Checkout` row first (so two concurrent
    checkin/override-return calls on the same checkout serialize and the
    second sees `checked_in_at` already set), then the `Asset` row before
    freeing it — same row-lock-first pattern as
    `apps.stock.services.apply_stock_txn`.
    """
    with transaction.atomic():
        locked_checkout = Checkout.objects.select_for_update().get(pk=checkout.pk)
        if locked_checkout.checked_in_at is not None:
            return locked_checkout, True  # already_checked_in: idempotent no-op

        locked_checkout.checked_in_at = timezone.now()
        locked_checkout.checkin_condition = checkin_condition
        locked_checkout.save(update_fields=["checked_in_at", "checkin_condition", "updated_at"])

        locked_asset = Asset.objects.select_for_update().get(pk=locked_checkout.asset_id)
        # Never clobber a status the asset has since moved on to on its own
        # (e.g. already retired/lost/maintenance) — only free it back to
        # AVAILABLE from IN_USE/RESERVED, the states checkout could have put
        # it in.
        if locked_asset.status in (Asset.Status.IN_USE, Asset.Status.RESERVED):
            locked_asset.status = Asset.Status.AVAILABLE
            locked_asset.save(update_fields=["status", "updated_at"])

    return locked_checkout, False


# --------------------------------------------------------------------------
# Viewset
# --------------------------------------------------------------------------


class CheckoutViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = CheckoutSerializer
    permission_classes = [CheckoutPermission]
    http_method_names = ["get", "post", "head", "options"]
    pagination_class = BoundedPageNumberPagination
    ordering_fields = ["due_at", "checked_out_at", "checked_in_at", "created_at"]

    def get_queryset(self):
        # Tenant-scoped manager, resolved per-request (golden-path step 2).
        qs = Checkout.objects.select_related(
            "asset", "asset__project", "asset__category", "user", "reservation"
        ).order_by("-checked_out_at")

        if self.action == "list":
            open_param = self.request.query_params.get("open", "").lower()
            if open_param == "true":
                # Backed by `reservations_checkout_open_partial`
                # (`WHERE checked_in_at IS NULL`) — T3.1.
                qs = qs.filter(checked_in_at__isnull=True)
            elif open_param == "false":
                qs = qs.filter(checked_in_at__isnull=False)

            overdue_param = self.request.query_params.get("overdue", "").lower()
            if overdue_param == "true":
                # Backed by `reservations_checkout_overdue_partial` — the
                # SAME predicate `Checkout.is_overdue` expresses in Python
                # (open AND past due_at), expressed here directly in SQL so
                # the filter runs at the DB, never "fetch all + filter in
                # Python".
                qs = qs.filter(checked_in_at__isnull=True, due_at__lt=timezone.now())

            # Scope-aware listing (docs/rbac.md §1), same rule as
            # apps.assets.api.AssetViewSet.get_queryset /
            # apps.stock.api.StockItemViewSet.get_queryset — PLUS a user
            # always sees their OWN checkouts (the T3.5 My Items screen),
            # regardless of whether their checkout-manage/override scope
            # would otherwise cover that asset's project.
            tenant_wide_manage, project_ids_manage = get_viewable_project_scope(
                self.request.user, CHECKOUT_MANAGE
            )
            tenant_wide_override, project_ids_override = get_viewable_project_scope(
                self.request.user, CHECKOUT_OVERRIDE
            )
            tenant_wide = tenant_wide_manage or tenant_wide_override
            project_ids = project_ids_manage | project_ids_override

            if not tenant_wide:
                own = Q(user_id=self.request.user.id)
                scoped = Q(asset__project_id__in=project_ids) if project_ids else Q(pk__in=[])
                qs = qs.filter(own | scoped)
        return qs

    def perform_create(self, serializer):
        checkout: Checkout = serializer.save()
        write_audit_log(
            tenant_id=checkout.tenant_id,
            actor=self.request.user,
            action=CHECKOUT_MANAGE,
            entity_type="checkout",
            entity_id=checkout.id,
            before=None,
            after={
                "asset_id": checkout.asset_id,
                "user_id": checkout.user_id,
                "due_at": checkout.due_at.isoformat(),
                "reservation_id": checkout.reservation_id,
            },
            ip=client_ip(self.request),
        )
        # T5.5: a new checkout moves an asset out of the pool -- bump the
        # dashboard cache so `currently_out` reflects it without waiting out
        # the TTL (see `apps.dashboard.cache` module docstring).
        invalidate_tenant_dashboard(checkout.tenant_id)

    @action(detail=True, methods=["post"])
    def checkin(self, request, pk=None):
        checkout = self.get_object()  # tenant + object-level RBAC (holder-only) already enforced
        body = CheckinSerializer(data=request.data)
        body.is_valid(raise_exception=True)

        before = {"checked_in_at": None, "asset_status": checkout.asset.status}
        updated, already_checked_in = perform_checkin(
            checkout=checkout, checkin_condition=body.validated_data["checkin_condition"]
        )

        if not already_checked_in:
            assert updated.checked_in_at is not None  # perform_checkin always sets it here
            write_audit_log(
                tenant_id=updated.tenant_id,
                actor=request.user,
                action=CHECKOUT_MANAGE,
                entity_type="checkout",
                entity_id=updated.id,
                before=before,
                after={
                    "checked_in_at": updated.checked_in_at.isoformat(),
                    "checkin_condition": updated.checkin_condition,
                },
                ip=client_ip(request),
            )
            # T5.5: `currently_out`/`overdue` are the most visibly stale
            # tiles (CLAUDE.md's own example) -- invalidate on every actual
            # state change, not just audited ones.
            invalidate_tenant_dashboard(updated.tenant_id)

        return Response(self.get_serializer(updated).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="override-return")
    def override_return(self, request, pk=None):
        checkout = self.get_object()  # tenant + `checkout.override` scoped RBAC already enforced
        body = CheckinSerializer(data=request.data)
        body.is_valid(raise_exception=True)

        before = {"checked_in_at": None, "asset_status": checkout.asset.status}
        updated, already_checked_in = perform_checkin(
            checkout=checkout, checkin_condition=body.validated_data["checkin_condition"]
        )

        if not already_checked_in:
            assert updated.checked_in_at is not None  # perform_checkin always sets it here
            # Always audited (docs/rbac.md §5, CLAUDE.md: every
            # `*.override` writes an AuditLog entry) — under the
            # `checkout.override` key specifically, distinct from a plain
            # self-checkin's `checkout.manage` entry, so the audit trail
            # shows this was a forced return, not a self-service one.
            write_audit_log(
                tenant_id=updated.tenant_id,
                actor=request.user,
                action=CHECKOUT_OVERRIDE,
                entity_type="checkout",
                entity_id=updated.id,
                before=before,
                after={
                    "checked_in_at": updated.checked_in_at.isoformat(),
                    "checkin_condition": updated.checkin_condition,
                },
                ip=client_ip(request),
            )
            invalidate_tenant_dashboard(updated.tenant_id)

        return Response(self.get_serializer(updated).data, status=status.HTTP_200_OK)
