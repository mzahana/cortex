"""Serializers for the money layer (M8 Phase 2).

Every derived figure — settled amounts, FX rates, variances, each project's
share — comes from `apps.finance.services` at representation time. None of it
is stored, and none of it is computed here; this module only shapes it.

The redaction rule (§7) is applied in `PaymentSerializer.to_representation`:
a caller who is not tenant-wide sees their own share itemized and everything
else as one unnamed total.
"""

from __future__ import annotations

from decimal import Decimal

from rest_framework import serializers

from .models import Payment, PaymentAttachment, Purchase, PurchaseAttachment
from .permissions import finance_scope
from .services import project_share_of_payment, resolve_payment, resolve_purchase


class PaymentAttachmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaymentAttachment
        fields = [
            "id",
            "payment",
            "storage_key",
            "filename",
            "content_type",
            "size",
            "uploaded_by",
            "created_at",
        ]
        read_only_fields = fields


class PurchaseAttachmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = PurchaseAttachment
        fields = [
            "id",
            "purchase",
            "storage_key",
            "filename",
            "content_type",
            "size",
            "uploaded_by",
            "created_at",
        ]
        read_only_fields = fields


class PurchaseSerializer(serializers.ModelSerializer):
    """One vendor receipt, with its derived reconciliation figures.

    `items_total`/`variance`/`is_balanced` are what drive the amber/green badge
    in the UI: they answer "do the items on this receipt add up to what the
    receipt says". `settled_amount`/`fx_rate` answer "what did it cost in the
    currency the bank actually charged".
    """

    attachments = PurchaseAttachmentSerializer(many=True, read_only=True)
    items_total = serializers.SerializerMethodField()
    overhead_total = serializers.SerializerMethodField()
    variance = serializers.SerializerMethodField()
    is_balanced = serializers.SerializerMethodField()
    resolved_settled_amount = serializers.SerializerMethodField()
    fx_rate = serializers.SerializerMethodField()
    settlement_currency = serializers.SerializerMethodField()

    class Meta:
        model = Purchase
        fields = [
            "id",
            "payment",
            "vendor",
            "vendor_order_number",
            "receipt_number",
            "date",
            "currency",
            "subtotal",
            "shipping",
            "tax",
            "total",
            "settled_amount",
            "resolved_settled_amount",
            "settlement_currency",
            "fx_rate",
            "items_total",
            "overhead_total",
            "variance",
            "is_balanced",
            "notes",
            "attachments",
            "created_by",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_by", "created_at", "updated_at"]

    def get_fields(self):
        fields = super().get_fields()
        # R4/F1: the writable FK is scoped through the tenant-scoped manager,
        # resolved lazily per request — never a class-body queryset.
        fields["payment"].queryset = Payment.objects.all()  # type: ignore[attr-defined]
        return fields

    def _resolution(self, purchase):
        cached = getattr(purchase, "_resolution", None)
        if cached is None:
            # A purchase serialized on its own has to resolve its parent charge
            # to know its settled share — the split depends on its siblings.
            if purchase.payment_id:
                for candidate in resolve_payment(purchase.payment).purchases:
                    if candidate.purchase_id == purchase.id:
                        cached = candidate
                        break
            if cached is None:
                cached = resolve_purchase(purchase, purchase.expenses.all(), None)
            purchase._resolution = cached
        return cached

    def get_items_total(self, obj) -> str:
        return str(self._resolution(obj).items_total)

    def get_overhead_total(self, obj) -> str:
        return str(self._resolution(obj).overhead_total)

    def get_variance(self, obj) -> str:
        return str(self._resolution(obj).variance)

    def get_is_balanced(self, obj) -> bool:
        return self._resolution(obj).is_balanced

    def get_resolved_settled_amount(self, obj) -> str | None:
        value = self._resolution(obj).settled_amount
        return None if value is None else str(value)

    def get_settlement_currency(self, obj) -> str | None:
        return self._resolution(obj).settlement_currency

    def get_fx_rate(self, obj) -> str | None:
        rate = self._resolution(obj).fx_rate
        return None if rate is None else str(rate)


class PaymentSerializer(serializers.ModelSerializer):
    """One bank charge, with its receipts and the reconciliation tally.

    `allocated`/`variance`/`is_balanced` are the running "X of Y accounted
    for" the UI puts front and centre — the single number that tells the user,
    and an auditor, whether a charge is fully explained.
    """

    attachments = PaymentAttachmentSerializer(many=True, read_only=True)
    purchases = serializers.SerializerMethodField()
    allocated = serializers.SerializerMethodField()
    variance = serializers.SerializerMethodField()
    is_balanced = serializers.SerializerMethodField()
    my_share = serializers.SerializerMethodField()
    other_projects_share = serializers.SerializerMethodField()

    class Meta:
        model = Payment
        fields = [
            "id",
            "amount",
            "currency",
            "paid_on",
            "method",
            "account_label",
            "statement_ref",
            "vendor",
            "notes",
            "purchases",
            "allocated",
            "variance",
            "is_balanced",
            "my_share",
            "other_projects_share",
            "attachments",
            "created_by",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_by", "created_at", "updated_at"]

    def _resolution(self, payment):
        cached = getattr(payment, "_resolution", None)
        if cached is None:
            cached = resolve_payment(payment)
            payment._resolution = cached
        return cached

    def _shares(self, payment) -> tuple[Decimal, Decimal]:
        request = self.context.get("request")
        user = getattr(request, "user", None) if request else None
        if user is None:
            return Decimal("0"), Decimal("0")
        tenant_wide, project_ids = finance_scope(user)
        if tenant_wide:
            # Nothing is hidden from a tenant-wide holder, so "mine" is the
            # whole charge and there is no redacted remainder to report.
            return self._resolution(payment).allocated, Decimal("0")
        return project_share_of_payment(self._resolution(payment), project_ids)

    def get_purchases(self, obj):
        """Only the receipts the caller is entitled to see.

        **This filter is load-bearing, not cosmetic.** Without it, a lead
        opening a shared charge received the FULL nested receipt list —
        including receipts belonging entirely to another project, with their
        vendor, order number and totals. `other_projects_share` was correctly
        redacted while the detail leaked one field over; a test
        (`test_project_lead_cannot_see_the_other_projects_detail`) caught it.

        A tenant-wide holder sees everything. Everyone else sees only receipts
        carrying at least one line booked to a project they lead — the same
        visible-set rule `apps.finance.permissions` applies to charges, one
        level down. A receipt SHARED between two projects stays visible to
        both, because each needs its header to reconcile their own items on
        it; the per-line detail is not part of this payload either way.
        """
        request = self.context.get("request")
        user = getattr(request, "user", None) if request else None
        purchases = list(obj.purchases.all())

        if user is not None:
            tenant_wide, project_ids = finance_scope(user)
            if not tenant_wide:
                purchases = [
                    purchase
                    for purchase in purchases
                    if any(line.project_id in project_ids for line in purchase.expenses.all())
                ]

        return PurchaseSerializer(purchases, many=True, context=self.context).data

    def get_allocated(self, obj) -> str:
        return str(self._resolution(obj).allocated)

    def get_variance(self, obj) -> str:
        return str(self._resolution(obj).variance)

    def get_is_balanced(self, obj) -> bool:
        return self._resolution(obj).is_balanced

    def get_my_share(self, obj) -> str:
        return str(self._shares(obj)[0])

    def get_other_projects_share(self, obj) -> str:
        """A single UNNAMED total — never a breakdown, never a project name.

        This is the §7 redaction: a lead reconciling a shared charge learns
        that the remainder went elsewhere, and nothing more.
        """
        return str(self._shares(obj)[1])
