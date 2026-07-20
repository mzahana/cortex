"""Consumables/stock models (T2.1): `StockItem`, `StockTxn`, `ReorderRequest`.

`docs/data-model.md` §2 ("Consumables & stock") is the field authority. All
three models are tenant-owned -> `TenantScopedModel` (T0.4's fail-closed base
manager), same golden-path rule as every other app.

**New tenant-owned tables for T2.3** (explicit list per this task's
instructions, db-migration-specialist to add RLS + the low-stock partial
index + the ledger-reconciliation/immutability DB constraints on top):
`stock_item` (`db_table="stock_stock_item"`), `stock_txn`
(`db_table="stock_stock_txn"`), `reorder_request`
(`db_table="stock_reorder_request"`).

## The consumable/durable invariant (`docs/data-model.md` §4)

"An asset is one or the other, never both." `StockItem` is a strict 1:1
extension of a CONSUMABLE `Asset` (`asset = OneToOneField(Asset, ...)` already
gives "at most one `StockItem` per `Asset`" at the DB level via the implicit
unique constraint on the FK). What the DB unique constraint can NOT express on
its own is "only a consumable asset may own one at all" — that half is
enforced here, at the app layer, in `StockItem.save()` (fail before the row is
ever written, not just on `full_clean()`, since not every caller — e.g. a
future service/import — is guaranteed to call `full_clean()` first). T2.3
adds the DB-level backstop (a `CHECK`/trigger tying `stock_item.asset_id` to
`assets_asset.is_consumable = true`), per this task's instructions — this is
NOT a substitute for that, same "central invariant, DB is the backstop"
relationship as tenant RLS.

## Ledger immutability (`docs/data-model.md` §2: "immutable ledger")

`StockTxn` is append-only: `save()` refuses ANY call where `self.pk` is
already set (i.e. anything but the original INSERT — an UPDATE attempt on an
existing row, whether via `some_txn.delta = X; some_txn.save()` or re-saving
a freshly-loaded instance), and `delete()` is blocked outright. A correction
is always a NEW row (`reason="correction"`) referencing the same
`stock_item`, never an edit of a past one. T2.3 adds the DB-level backstop (a
trigger rejecting UPDATE/DELETE outright, the same belt-and-suspenders
pattern as `apps.audit.AuditLog`) — this app-layer guard is not a substitute
for that.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models

from apps.assets.models import Asset
from apps.catalog.models import Location
from apps.tenancy.models import TenantScopedModel


class StockItem(TenantScopedModel):
    """1:1 extension of a CONSUMABLE `Asset` (`docs/data-model.md` §2).

    `quantity_on_hand` is the derived-and-reconciled figure: the ledger
    (`StockTxn`) is the source of truth, and this field only exists so reads
    (list/detail, the low-stock scan) don't have to re-sum the ledger on
    every request. It is written ONLY by the ledger-apply path (T2.2's
    `apps.stock.services`, inside the same transaction as the `StockTxn`
    insert that changed it) — never accepted directly from a client; see
    `apps.stock.serializers.StockItemSerializer` (`read_only`) and
    `recompute_quantity_on_hand()` below (the ledger-sum reconciliation this
    task leaves for T2.2/T2.3 to wire in atomically under a row lock).
    """

    asset = models.OneToOneField(
        Asset,
        on_delete=models.PROTECT,
        related_name="stock_item",
        help_text="PROTECT: a consumable asset with stock history cannot be "
        "deleted out from under it (assets are never hard-deleted anyway, "
        "docs/data-model.md §4, but this keeps the FK contract explicit).",
    )
    unit_of_measure = models.CharField(max_length=32)
    quantity_on_hand = models.IntegerField(
        default=0,
        help_text="Derived/reconciled against the StockTxn ledger sum "
        "(docs/data-model.md §2) — never set directly by client writes.",
    )
    reorder_threshold = models.IntegerField(default=0)
    reorder_target = models.IntegerField(default=0)
    bin_location = models.ForeignKey(
        Location,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="stock_items",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "stock_stock_item"
        indexes = [
            models.Index(fields=["tenant"]),
            # T2.3 also adds the PARTIAL index `stock_item (tenant_id)` WHERE
            # `quantity_on_hand <= reorder_threshold` (docs/data-model.md §3,
            # the low-stock scan) — this plain composite is kept here as the
            # cheap non-partial baseline; T2.3 layers the partial one on top.
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"stock:{self.asset_id}"

    def clean(self) -> None:
        super().clean()
        if self.asset_id and not self.asset.is_consumable:
            raise ValidationError(
                {
                    "asset": "Only a consumable asset may own a StockItem "
                    "(docs/data-model.md §4: an asset is consumable or "
                    "durable, never both)."
                }
            )

    def save(self, *args, **kwargs):
        # Enforced here (not only in `clean()`/`full_clean()`) so this is a
        # fail-closed model invariant regardless of whether a caller
        # remembers to call `full_clean()` first — same reasoning as
        # `StockTxn.save()` below. T2.3 backstops this at the DB level.
        if self.asset_id and not self.asset.is_consumable:
            raise ValidationError(
                {
                    "asset": "Only a consumable asset may own a StockItem "
                    "(docs/data-model.md §4: an asset is consumable or "
                    "durable, never both)."
                }
            )
        super().save(*args, **kwargs)

    def recompute_quantity_on_hand(self) -> int:
        """Sum the `StockTxn` ledger for this item — the reconciliation
        T2.3's DB constraint/trigger enforces permanently; exposed here as a
        plain read so T2.2's ledger-apply service (and tests) have one
        shared definition of "what the ledger says the quantity should be"
        rather than re-deriving the aggregate in more than one place.
        """
        total = self.stock_txns.aggregate(total=models.Sum("delta"))["total"]
        return total or 0


class StockTxn(TenantScopedModel):
    """Immutable ledger of every quantity change against a `StockItem`
    (`docs/data-model.md` §2). Append-only — see module docstring.
    """

    class Reason(models.TextChoices):
        RECEIVE = "receive", "Receive"
        CONSUME = "consume", "Consume"
        ADJUST = "adjust", "Adjust"
        CORRECTION = "correction", "Correction"

    stock_item = models.ForeignKey(StockItem, on_delete=models.CASCADE, related_name="stock_txns")
    delta = models.IntegerField(help_text="Signed quantity change (positive=in, negative=out).")
    reason = models.CharField(max_length=16, choices=Reason.choices)
    ref = models.CharField(max_length=255, blank=True, default="", help_text="Free reference.")
    actor = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="stock_txns",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "stock_stock_txn"
        indexes = [
            models.Index(fields=["tenant", "stock_item", "created_at"]),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.stock_item_id}:{self.reason}:{self.delta}"

    def save(self, *args, **kwargs):
        # Append-only (module docstring): any call where `self.pk` is
        # already set is necessarily an attempted UPDATE of a row that was
        # (or would be) already written — the very first `save()` on a brand
        # new instance always has `pk is None` at this point, so this only
        # ever blocks a second save. T2.3 adds the DB-level trigger backstop.
        if self.pk is not None:
            raise ValidationError(
                "StockTxn is append-only: an existing ledger row cannot be "
                "updated. Write a new row (reason='correction') instead."
            )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("StockTxn is append-only: ledger rows cannot be deleted.")


class ReorderRequest(TenantScopedModel):
    """Lightweight reorder workflow, linked to a `StockItem` (and, through
    it, the consumable `Asset`) — `docs/data-model.md` §2.

    ASSUMPTION (flagged): `docs/data-model.md` lists `stock_item_id,
    requested_by, quantity, status, note` but the task instructions
    additionally call for a requester/**approver** pair and status
    timestamps to support the approve/reject workflow (T2.2) and its audit
    trail (`docs/rbac.md` §5's `reorder.approve`). `approved_by` /
    `decided_at` are therefore added here as a reasonable, additive
    extension of the documented shape (not a conflicting reinterpretation of
    it) — flagged for db-migration-specialist/code-reviewer.
    """

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        APPROVED = "approved", "Approved"
        ORDERED = "ordered", "Ordered"
        RECEIVED = "received", "Received"
        CANCELLED = "cancelled", "Cancelled"

    # Only-valid forward transitions (docs/tasks/M2-consumables-stock.md T2.1:
    # "status open→approved→ordered→received→cancelled (valid transitions
    # only)"). `cancelled` is reachable from any non-terminal state (an open,
    # approved, or ordered request can still be called off); `received` and
    # `cancelled` are terminal — nothing transitions out of them. Kept here
    # (not duplicated in the serializer) as the single source of truth.
    VALID_TRANSITIONS: dict[str, frozenset[str]] = {
        Status.OPEN: frozenset({Status.APPROVED, Status.CANCELLED}),
        Status.APPROVED: frozenset({Status.ORDERED, Status.CANCELLED}),
        Status.ORDERED: frozenset({Status.RECEIVED, Status.CANCELLED}),
        Status.RECEIVED: frozenset(),
        Status.CANCELLED: frozenset(),
    }

    # Terminal states (nothing transitions OUT of them, `VALID_TRANSITIONS`
    # above) — code-review follow-up (T2.2): a request in one of these states
    # is immutable outright, not just "no further status transition". See
    # `apps.stock.serializers.ReorderRequestSerializer.validate` for the
    # plain-field-edit guard this backs.
    TERMINAL_STATUSES: frozenset[str] = frozenset({Status.RECEIVED, Status.CANCELLED})

    stock_item = models.ForeignKey(
        StockItem, on_delete=models.PROTECT, related_name="reorder_requests"
    )
    requested_by = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reorder_requests_made",
    )
    approved_by = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reorder_requests_approved",
    )
    quantity = models.PositiveIntegerField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN)
    note = models.TextField(blank=True, default="")
    requested_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "stock_reorder_request"
        indexes = [
            models.Index(fields=["tenant", "stock_item", "status"]),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.stock_item_id}:{self.status}"

    def validate_transition(self, new_status: str) -> None:
        """Raise if `new_status` is not a valid forward move from the
        CURRENT (pre-save, DB) status. Called from
        `apps.stock.serializers.ReorderRequestSerializer.validate` (T2.1) and
        again from T2.2's approve/reject service as the single source of
        truth for legal transitions.
        """
        if new_status == self.status:
            return  # no-op PATCH (e.g. re-sending the same status) is allowed
        allowed = self.VALID_TRANSITIONS.get(self.status, frozenset())
        if new_status not in allowed:
            raise ValidationError(
                {
                    "status": (
                        f"Cannot transition a '{self.status}' reorder request to "
                        f"'{new_status}'. Valid next states: "
                        f"{sorted(allowed) or 'none (terminal)'}."
                    )
                }
            )
