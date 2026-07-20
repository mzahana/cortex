"""Reservation & checkout models (T3.1): `Reservation`, `Checkout`.

`docs/data-model.md` §2 ("Reservations & checkout") is the field authority.
Both models are tenant-owned -> `TenantScopedModel` (T0.4's fail-closed base
manager), same golden-path rule as every other app (assets, stock, ...).

**New tenant-owned tables** (db-migration-specialist wires RLS + the specialized
indexes + the F4 exclusion constraint on top, in `0002`):
`reservation` (`db_table="reservations_reservation"`) and `checkout`
(`db_table="reservations_checkout"`).

## The F4 no-overlap invariant (`docs/data-model.md` §2/§3, `docs/features.md` F4)

Two ACTIVE reservations for the same asset must never overlap in time. This is
enforced at the DB level by a GiST EXCLUDE constraint (added in `0002`, needs the
`btree_gist` extension from M0):

    EXCLUDE USING gist (asset_id WITH =, tstzrange(start_at, end_at) WITH &&)
        WHERE (status IN ('pending', 'approved', 'fulfilled'))

The WHERE clause restricts participation to the ACTIVE statuses only
(`ACTIVE_STATUSES` below) — a `cancelled`/`rejected`/`expired` reservation drops
out of the constraint so the freed window can be re-booked. `ACTIVE_STATUSES` is
the single source of truth for that set: the T3.2 service layer's conflict
pre-check MUST key off the SAME tuple the DB constraint's WHERE clause uses, so
the app-level check and the DB backstop can never disagree (same
"central invariant, DB is the backstop" relationship as tenant RLS). If you edit
this tuple, edit the constraint WHERE clause in migration `0002` to match.

## `Checkout.is_overdue` is derived, never stored (`docs/data-model.md` §2)

`docs/data-model.md` lists `is_overdue` on Checkout, but — unlike
`StockItem.quantity_on_hand`, which is a *reconciled cache* of an authoritative
ledger sum kept correct by a DB trigger — "open and past due" is a pure function
of two columns already on the row (`checked_in_at IS NULL` and `due_at < now()`)
plus wall-clock time. A stored boolean would be wrong the instant the clock
crosses `due_at` (nothing writes the row at that moment), so there is nothing to
reconcile and a stored column would only ever go stale. It is therefore a
computed `@property` here, and the T3.3 `GET /checkouts?overdue=true` filter
expresses the same predicate directly in SQL (`checked_in_at IS NULL AND
due_at < now()`), backed by the open-items partial index from `0002`.
"""

from __future__ import annotations

from django.db import models
from django.utils import timezone

from apps.assets.models import Asset
from apps.projects.models import Project
from apps.tenancy.models import TenantScopedModel


class Reservation(TenantScopedModel):
    """Durable-asset booking (`docs/data-model.md` §2). Overlap of two ACTIVE
    reservations for the same asset is rejected at the DB by the `0002` GiST
    exclusion constraint (F4)."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        CANCELLED = "cancelled", "Cancelled"
        FULFILLED = "fulfilled", "Fulfilled"
        EXPIRED = "expired", "Expired"

    # The statuses that PARTICIPATE in the no-overlap exclusion constraint
    # (`0002`'s WHERE clause). A reservation in any other status (rejected,
    # cancelled, expired) drops out of the constraint so its window can be
    # re-booked. Single source of truth: the T3.2 conflict pre-check keys off
    # this exact set, and `0002`'s constraint WHERE clause mirrors it.
    ACTIVE_STATUSES: tuple[str, ...] = (
        Status.PENDING,
        Status.APPROVED,
        Status.FULFILLED,
    )

    asset = models.ForeignKey(
        Asset,
        on_delete=models.PROTECT,
        related_name="reservations",
        help_text="PROTECT: assets are never hard-deleted (docs/data-model.md "
        "§4); keeps the FK contract explicit.",
    )
    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.PROTECT,
        related_name="reservations",
        help_text="The requester/holder. PROTECT: this is ownership, not an "
        "audit actor — a user holding reservations is soft-deactivated "
        "(is_active=False), never hard-deleted out from under their bookings.",
    )
    project = models.ForeignKey(
        Project,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reservations",
        help_text="Optional project the reservation is charged to (general "
        "pool when NULL, docs/data-model.md §4).",
    )
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    approver = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reservations_approved",
        help_text="The user who approved/rejected (docs/rbac.md §4). SET_NULL "
        "(actor-style reference), same pattern as ReorderRequest.approved_by.",
    )
    approval_note = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "reservations_reservation"
        indexes = [
            # Calendar feed `GET /reservations?from&to` scoped to a single
            # asset over a time window (docs/api-and-ui.md), and the per-asset
            # overlap pre-check.
            models.Index(fields=["tenant", "asset", "start_at"]),
            # Tenant-wide calendar range scan (from&to without an asset filter).
            models.Index(fields=["tenant", "start_at", "end_at"]),
            # Approvals queue: pending reservations in the approver's scope
            # (docs/rbac.md §4, the T3.4 Approvals screen).
            models.Index(fields=["tenant", "status"]),
        ]
        constraints = [
            # A reservation window must be non-empty and forward. tstzrange with
            # lower > upper raises at the DB; equal bounds is an empty range that
            # never overlaps. Reject both so every stored window is meaningful
            # and the exclusion constraint is exercised as intended.
            models.CheckConstraint(
                check=models.Q(end_at__gt=models.F("start_at")),
                name="reservation_end_after_start",
            ),
        ]
        ordering = ["-start_at"]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"reservation:{self.asset_id}:{self.status}"


class Checkout(TenantScopedModel):
    """Actual possession of a durable asset (`docs/data-model.md` §2). An open
    checkout is `checked_in_at IS NULL`; overdue is `is_overdue` below."""

    asset = models.ForeignKey(
        Asset,
        on_delete=models.PROTECT,
        related_name="checkouts",
        help_text="PROTECT: assets are never hard-deleted (docs/data-model.md §4).",
    )
    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.PROTECT,
        related_name="checkouts",
        help_text="The holder. PROTECT — ownership, not an audit actor (see " "Reservation.user).",
    )
    reservation = models.ForeignKey(
        Reservation,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="checkouts",
        help_text="Optional originating reservation (a walk-up checkout has "
        "none). SET_NULL keeps the possession record if the reservation row "
        "is later removed.",
    )
    checked_out_at = models.DateTimeField()
    due_at = models.DateTimeField()
    checked_in_at = models.DateTimeField(null=True, blank=True)
    checkout_condition = models.TextField(blank=True, default="")
    checkin_condition = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "reservations_checkout"
        indexes = [
            # Per-asset possession history / "who has this asset now".
            models.Index(fields=["tenant", "asset"]),
            # A user's own items (the T3.5 My Items screen).
            models.Index(fields=["tenant", "user"]),
            # NOTE: the open-items partial index
            # `(tenant_id, checked_in_at) WHERE checked_in_at IS NULL` and the
            # overdue partial index `(tenant_id, due_at) WHERE checked_in_at IS
            # NULL` are added in migration `0002` (partial indexes -> RunSQL,
            # matching the stock app's low-stock partial-index precedent).
        ]
        constraints = [
            models.CheckConstraint(
                check=models.Q(due_at__gt=models.F("checked_out_at")),
                name="checkout_due_after_checkout",
            ),
        ]
        ordering = ["-checked_out_at"]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"checkout:{self.asset_id}:{'open' if self.is_open else 'closed'}"

    @property
    def is_open(self) -> bool:
        """An open checkout is one not yet checked in (`docs/data-model.md`
        §2). The open-items partial index in `0002` keys off exactly this
        predicate (`checked_in_at IS NULL`)."""
        return self.checked_in_at is None

    @property
    def is_overdue(self) -> bool:
        """Open AND past `due_at` (T3.3: "Overdue = open past due_at"). Derived,
        never stored — see the module docstring. The T3.3
        `GET /checkouts?overdue=true` filter expresses the same predicate in
        SQL (`checked_in_at IS NULL AND due_at < now()`)."""
        return self.is_open and self.due_at < timezone.now()
