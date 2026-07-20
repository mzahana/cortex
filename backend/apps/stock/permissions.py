"""Scoped RBAC enforcement for the stock endpoints (T2.2).

**Stock is project-scoped via its `Asset`** (task instructions,
`docs/rbac.md` §1's union-of-memberships rule): every scope decision below
resolves the underlying `Asset.project_id` — through `StockItem.asset` for
`StockItemViewSet`, and through `ReorderRequest.stock_item.asset` for
`ReorderRequestViewSet` — and applies the SAME two-phase pattern
`apps.assets.permissions.AssetPermission` already established (see that
module's docstring for the full "why two-phase" reasoning): a permissive
"holds it somewhere" check at `has_permission()` (before the object exists),
then the real, scope-correct check against the object's actual project at
`has_object_permission()`. This is what avoids reintroducing the M0
pure-ProjectLead over-deny bug.

**There is no dedicated `stock.view`/"view reorder" key in `docs/rbac.md`**
— stock/reorder rows are read-projections of a consumable `Asset`, so
reads are gated by `asset.view` (same permission, same scope, as viewing the
asset itself). Mutations use the exact keys the task maps from the ledger
`reason` / reorder transition:

| Action                                  | Permission key      |
|------------------------------------------|----------------------|
| `GET /stock`, `GET /stock/{id}`           | `asset.view`         |
| `POST /stock/{id}/txn` reason=`receive`   | `stock.adjust`       |
| `POST /stock/{id}/txn` reason=`adjust`    | `stock.adjust`       |
| `POST /stock/{id}/txn` reason=`correction`| `stock.adjust`       |
| `POST /stock/{id}/txn` reason=`consume`   | `stock.consume`      |
| `GET /reorder-requests`(`/{id}`)          | `asset.view`         |
| `POST /reorder-requests` (create)         | `reorder.request`    |
| `PATCH .../{id}` → status=`approved`      | `reorder.approve`    |
| `PATCH .../{id}` → status in {`ordered`,`received`} | `reorder.approve` |
| `PATCH .../{id}` → status=`cancelled`     | `reorder.approve` OR the |
|                                            | original requester cancelling their own request |
| `PATCH .../{id}` non-status edit          | `reorder.approve` OR the |
| (quantity/note)                           | original requester |
"""

from __future__ import annotations

from rest_framework.permissions import BasePermission

from apps.rbac.permission_keys import (
    ASSET_VIEW,
    REORDER_APPROVE,
    REORDER_REQUEST,
    STOCK_ADJUST,
    STOCK_CONSUME,
)
from apps.rbac.services import user_has_permission, user_has_permission_in_any_scope

from .models import ReorderRequest, StockItem, StockTxn

REASON_PERMISSION_MAP: dict[str, str] = {
    StockTxn.Reason.RECEIVE.value: STOCK_ADJUST,
    StockTxn.Reason.ADJUST.value: STOCK_ADJUST,
    StockTxn.Reason.CORRECTION.value: STOCK_ADJUST,
    StockTxn.Reason.CONSUME.value: STOCK_CONSUME,
}

# Transitions that always require `reorder.approve` (procurement-decision
# transitions, not a self-service action) — everything except a `cancelled`
# transition, which the original requester may also perform on their own
# request (see module docstring table).
APPROVER_ONLY_TRANSITIONS = frozenset(
    {ReorderRequest.Status.APPROVED, ReorderRequest.Status.ORDERED, ReorderRequest.Status.RECEIVED}
)


class StockItemPermission(BasePermission):
    """`StockItemViewSet`: list/retrieve gated by `asset.view`; the `txn`
    action's required key depends on the request body's `reason` (see
    `REASON_PERMISSION_MAP`).
    """

    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False

        action = getattr(view, "action", "") or ""
        if action in ("list", "retrieve"):
            return user_has_permission_in_any_scope(user, ASSET_VIEW)

        if action == "txn":
            reason = request.data.get("reason")
            permission_key = REASON_PERMISSION_MAP.get(reason)
            if permission_key is None:
                # Unrecognized/missing reason: let the request through so the
                # serializer's `choices` validation returns a clean 400
                # rather than this permission class returning a 403 for what
                # is really a validation error.
                return True
            return user_has_permission_in_any_scope(user, permission_key)

        return False  # fail-closed: unmapped action

    def has_object_permission(self, request, view, obj: StockItem) -> bool:
        user = request.user
        action = getattr(view, "action", "") or ""
        project_id = obj.asset.project_id

        if action in ("list", "retrieve"):
            return user_has_permission(user, ASSET_VIEW, project=project_id)

        if action == "txn":
            reason = request.data.get("reason")
            permission_key = REASON_PERMISSION_MAP.get(reason)
            if permission_key is None:
                return True  # same "let the serializer 400 it" reasoning as above
            return user_has_permission(user, permission_key, project=project_id)

        return False


def _resolve_target_project_id_for_stock_item(stock_item_id) -> tuple[bool, int | None]:
    """Resolve the target `StockItem`'s underlying `Asset.project_id` for a
    `create` request, re-scoped through the tenant-scoped `StockItem.objects`
    manager (R4: never trust a client-supplied id blindly).

    Returns `(found, project_id)` — `found=False` means the id didn't
    resolve inside the caller's tenant at all (a bogus/cross-tenant id);
    the caller treats that as "no permission" rather than guessing a scope,
    the same as `apps.assets.permissions._resolve_target_project_id` treats
    an unresolvable project id as "general pool" for permission-check
    purposes (the actual 404 still comes from the serializer's own
    tenant-scoped FK field).
    """
    if stock_item_id in (None, "", "null"):
        return False, None
    try:
        stock_item = StockItem.objects.select_related("asset").get(pk=stock_item_id)
    except (StockItem.DoesNotExist, ValueError, TypeError):
        return False, None
    return True, stock_item.asset.project_id


class ReorderRequestPermission(BasePermission):
    """`ReorderRequestViewSet`: list/retrieve gated by `asset.view`; create
    gated by `reorder.request` (scoped to the target stock item's asset's
    project); status-transition/edit `PATCH` gated per the module docstring
    table.
    """

    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False

        action = getattr(view, "action", "") or ""
        if action in ("list", "retrieve"):
            return user_has_permission_in_any_scope(user, ASSET_VIEW)

        if action == "create":
            found, project_id = _resolve_target_project_id_for_stock_item(
                request.data.get("stock_item")
            )
            if not found:
                # Let the serializer's tenant-scoped FK field produce the
                # clean 400 for a missing/bogus/cross-tenant `stock_item`
                # rather than this permission class guessing at a scope.
                return True
            return user_has_permission(user, REORDER_REQUEST, project=project_id)

        if action in ("update", "partial_update"):
            # Provisional "holds either key somewhere" gate (two-phase
            # pattern); `has_object_permission` makes the real, scope- and
            # transition-correct decision once the object is known.
            return user_has_permission_in_any_scope(
                user, REORDER_APPROVE
            ) or user_has_permission_in_any_scope(user, REORDER_REQUEST)

        return False

    def has_object_permission(self, request, view, obj: ReorderRequest) -> bool:
        user = request.user
        action = getattr(view, "action", "") or ""
        project_id = obj.stock_item.asset.project_id

        if action in ("list", "retrieve"):
            return user_has_permission(user, ASSET_VIEW, project=project_id)

        if action in ("update", "partial_update"):
            new_status = request.data.get("status")
            is_requester = obj.requested_by_id == user.id

            if new_status is None:
                # A non-status edit (quantity/note): the original requester
                # may edit their own still-open request; anyone holding
                # `reorder.approve` in this scope may also amend it.
                return is_requester or user_has_permission(
                    user, REORDER_APPROVE, project=project_id
                )

            if new_status == ReorderRequest.Status.CANCELLED:
                return is_requester or user_has_permission(
                    user, REORDER_APPROVE, project=project_id
                )

            if new_status in APPROVER_ONLY_TRANSITIONS:
                return user_has_permission(user, REORDER_APPROVE, project=project_id)

            # Anything else (e.g. re-sending the current status, or a value
            # the model's own `validate_transition` will reject as invalid)
            # falls back to the approver check — never grants a requester an
            # unlisted transition.
            return user_has_permission(user, REORDER_APPROVE, project=project_id)

        return False
