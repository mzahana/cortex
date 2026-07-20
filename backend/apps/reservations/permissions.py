"""Scoped RBAC enforcement for the reservation endpoints (T3.2).

**Reservations are project-scoped via their `Asset`** (`docs/rbac.md` §1's
union-of-memberships rule, §4's per-category approval config) -- every scope
decision below resolves `Reservation.asset.project_id` and applies the SAME
two-phase pattern `apps.assets.permissions.AssetPermission` /
`apps.stock.permissions` already established: a permissive "holds it
somewhere" check at `has_permission()` (before the object exists), then the
real, scope-correct check against the object's actual project at
`has_object_permission()`.

| Action                                         | Permission key           |
|-------------------------------------------------|---------------------------|
| `GET /reservations`, `GET /reservations/{id}`   | `asset.view` (1)          |
| `POST /reservations` (create)                   | `reservation.create`      |
| `POST /reservations/{id}/approve`               | `reservation.approve`     |
| `POST /reservations/{id}/reject`                | `reservation.approve`     |
| `POST /reservations/{id}/cancel`                | `reservation.approve` or own booking (2) |

1. Reservations are a read-projection of the asset they book -- same
   reasoning as `apps.stock.permissions`'s "no dedicated stock.view key".
2. Either the scoped approver, or the requester cancelling their own booking.

**General-pool assets** (`asset.project_id IS NULL`) requiring approval are
approved by Admins only (`docs/rbac.md` §4): this falls out of
`apps.rbac.services.get_effective_permissions`'s union-of-memberships rule
automatically -- passing `project=None` to `user_has_permission` only ever
matches a TENANT-WIDE membership (Admin), never a project-scoped one (a
ProjectLead's membership is scoped to a specific project id, which can never
equal `None`) -- so no special-casing is needed here, same as
`apps.stock.permissions.StockItemPermission` relies on for `stock.adjust`.
"""

from __future__ import annotations

from rest_framework.permissions import BasePermission

from apps.assets.models import Asset
from apps.rbac.permission_keys import ASSET_VIEW, RESERVATION_APPROVE, RESERVATION_CREATE
from apps.rbac.services import user_has_permission, user_has_permission_in_any_scope

from .models import Reservation


def _resolve_target_project_id_for_asset(asset_id) -> tuple[bool, int | None]:
    """Resolve the target `Asset`'s `project_id` for a `create` request,
    re-scoped through the tenant-scoped `Asset.objects` manager (R4: never
    trust a client-supplied id blindly) -- same pattern as
    `apps.stock.permissions._resolve_target_project_id_for_stock_item`.

    Returns `(found, project_id)` -- `found=False` means the id didn't
    resolve inside the caller's tenant at all; the caller treats that as "no
    permission" and lets the serializer's own tenant-scoped FK field produce
    the clean 400/404 for a missing/bogus/cross-tenant asset id.
    """
    if asset_id in (None, "", "null"):
        return False, None
    try:
        asset = Asset.objects.only("id", "project_id").get(pk=asset_id)
    except (Asset.DoesNotExist, ValueError, TypeError):
        return False, None
    return True, asset.project_id


class ReservationPermission(BasePermission):
    """`ReservationViewSet`: list/retrieve gated by `asset.view`; create
    gated by `reservation.create` (scoped to the target asset's project);
    approve/reject gated by `reservation.approve` (scoped); cancel gated by
    `reservation.approve` OR the reservation's own requester.
    """

    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False

        action = getattr(view, "action", "") or ""

        if action in ("list", "retrieve"):
            return user_has_permission_in_any_scope(user, ASSET_VIEW)

        if action == "create":
            found, project_id = _resolve_target_project_id_for_asset(request.data.get("asset"))
            if not found:
                # Let the serializer's tenant-scoped FK field produce the
                # clean 400 for a missing/bogus/cross-tenant `asset`, rather
                # than this permission class guessing at a scope.
                return True
            return user_has_permission(user, RESERVATION_CREATE, project=project_id)

        if action in ("approve", "reject"):
            # Provisional "holds it somewhere" gate (two-phase pattern);
            # `has_object_permission` makes the real, scope-correct decision
            # once the object (and its asset's actual project) is known.
            return user_has_permission_in_any_scope(user, RESERVATION_APPROVE)

        if action == "cancel":
            # Either half of the OR could be true without holding the OTHER
            # key anywhere (e.g. a plain Member who only ever holds
            # `reservation.create`, cancelling their own booking) -- so the
            # provisional gate must accept "holds `reservation.create`
            # ANYWHERE" too, not just `reservation.approve`. The real
            # decision (own booking, or approver in the CORRECT scope) is
            # made in `has_object_permission`.
            return user_has_permission_in_any_scope(
                user, RESERVATION_APPROVE
            ) or user_has_permission_in_any_scope(user, RESERVATION_CREATE)

        return False  # fail-closed: unmapped action

    def has_object_permission(self, request, view, obj: Reservation) -> bool:
        user = request.user
        action = getattr(view, "action", "") or ""
        project_id = obj.asset.project_id

        if action in ("list", "retrieve"):
            return user_has_permission(user, ASSET_VIEW, project=project_id)

        if action in ("approve", "reject"):
            return user_has_permission(user, RESERVATION_APPROVE, project=project_id)

        if action == "cancel":
            is_owner = obj.user_id == user.id
            return is_owner or user_has_permission(user, RESERVATION_APPROVE, project=project_id)

        return False
