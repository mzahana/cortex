"""The **order** API — one nested endpoint for the whole thing (M8 Phase 2, rev).

The user's mental model, in their words: *"the expense itself is the order made
from a vendor which can have splits and each split has items."* So there is one
record to create, with things nested inside it:

    Order    Amazon, 2 Mar, SAR 4,631.10 taken from the bank
      ├ Split 1   receipt scan, USD 400.00
      │   ├ Item  GPU    USD 350.00  -> asset
      │   └ Item  Cable  USD   9.00
      └ Split 2   receipt scan, USD 834.56
          └ Item  Jetson USD 834.56  -> asset

Underneath, those are `finance.Payment`, `finance.Purchase` and
`projects.Expense` — the same three records as before. What changed is that the
user never operates them separately. An earlier cut exposed them as two
disconnected workflows ("create an expense" and "create a bank charge"), which
made the user assemble the structure by hand across screens. That was the wrong
shape, and this module exists to replace it.

**One request, one transaction.** The client sends the whole tree and gets the
whole tree back. Doing this as three or more separate calls from the browser
would mean a failure part-way through leaves an order with no items, or items
with no receipt — exactly the half-entered state a financial record must never
be in. `ATOMIC_REQUESTS` covers the write, so it is all-or-nothing.

**Splits are optional in the UI, never absent in the data.** A simple order —
one delivery, no split — still stores exactly one `Purchase`, created
implicitly. That keeps one code path for reconciliation and reporting instead
of a special case for "orders with no split", while the user never sees the
concept unless their order actually arrived in pieces.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.assets.models import Asset
from apps.audit.services import client_ip, write_audit_log
from apps.jobs.models import Job
from apps.jobs.serializers import JobSerializer
from apps.projects.models import Expense, ExpenseCategory, Project
from apps.projects.services import sync_expense_asset_links
from apps.rbac.permission_keys import PAYMENT_MANAGE

from .models import Payment, Purchase
from .permissions import PaymentPermission
from .serializers import PaymentAttachmentSerializer, PurchaseAttachmentSerializer
from .services import order_number_expr, resolve_payment
from .tasks import generate_order_pack_pdf


class OrderItemSerializer(serializers.Serializer):
    """One item inside a split. Backed by `projects.Expense`."""

    id = serializers.IntegerField(required=False)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    amount = serializers.DecimalField(max_digits=14, decimal_places=2)
    category = serializers.PrimaryKeyRelatedField(
        queryset=ExpenseCategory.all_objects.none(), required=False, allow_null=True
    )
    # ONE asset per item, not a list. An item IS a single line on a receipt —
    # one thing bought — so a set of assets never made sense here. (The
    # many-to-many machinery underneath, `ExpenseAssetLink`, stays: it still
    # serves the "one service covering several instruments" case reachable
    # elsewhere, and it is what stores this single link.)
    asset = serializers.PrimaryKeyRelatedField(
        required=False, allow_null=True, queryset=Asset.all_objects.none()
    )


class OrderSplitSerializer(serializers.Serializer):
    """One shipment/receipt within an order. Backed by `finance.Purchase`."""

    id = serializers.IntegerField(required=False)
    receipt_number = serializers.CharField(required=False, allow_blank=True, default="")
    date = serializers.DateField(required=False, allow_null=True)
    currency = serializers.CharField(max_length=3, required=False, allow_blank=True, default="")
    shipping = serializers.DecimalField(
        max_digits=14, decimal_places=2, required=False, default=Decimal("0")
    )
    tax = serializers.DecimalField(
        max_digits=14, decimal_places=2, required=False, default=Decimal("0")
    )
    total = serializers.DecimalField(
        max_digits=14, decimal_places=2, required=False, allow_null=True
    )
    settled_amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, required=False, allow_null=True
    )
    items = OrderItemSerializer(many=True, required=False)
    attachments = PurchaseAttachmentSerializer(many=True, read_only=True)


class OrderSerializer(serializers.ModelSerializer):
    """The whole order, read and written as one nested document."""

    splits = OrderSplitSerializer(many=True, required=False)
    attachments = PaymentAttachmentSerializer(many=True, read_only=True)
    # Derived reconciliation figures — see `apps.finance.services`. `variance`
    # is what the form's running tally shows; it warns, it never blocks.
    # Position in statement order within the project — the same "Order N" the
    # project report and the downloaded pack filename use. Annotated on the
    # queryset (`order_number_expr`), so listing N orders stays one query.
    number = serializers.IntegerField(read_only=True)
    items_total_settled = serializers.SerializerMethodField()
    variance = serializers.SerializerMethodField()
    is_balanced = serializers.SerializerMethodField()

    class Meta:
        model = Payment
        fields = [
            "id",
            "number",
            "project",
            "vendor",
            "paid_on",
            "amount",
            "currency",
            "method",
            "account_label",
            "statement_ref",
            "notes",
            "splits",
            "attachments",
            "items_total_settled",
            "variance",
            "is_balanced",
            "created_by",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_by", "created_at", "updated_at"]

    def get_fields(self):
        fields = super().get_fields()
        # R4/F1: every writable relation is scoped through its tenant-scoped
        # manager, resolved lazily per request — never at class-definition time
        # (which runs before any tenant exists and would fail closed).
        fields["project"].queryset = Project.objects.all()  # type: ignore[attr-defined]
        # The COLUMN is nullable (a migration-safety concession, see
        # `0002_m8_order_project`), which makes DRF infer an optional field.
        # Every order created through this API must have a project, so override
        # that inference here rather than relying on `validate_project`, which
        # only runs when the key is present at all.
        fields["project"].required = True
        fields["project"].allow_null = False
        item_fields = fields["splits"].child.fields["items"].child.fields  # type: ignore[attr-defined]
        item_fields["category"].queryset = ExpenseCategory.objects.all()

        # An item may only link assets funded by THIS order's project, or
        # unassigned ones — the same rule `ExpenseSerializer` enforces, since
        # an order buys its own project's assets and nobody else's.
        project = self.initial_data.get("project") if hasattr(self, "initial_data") else None
        project = project or getattr(self.instance, "project_id", None)
        if project:
            from django.db.models import Q

            item_fields["asset"].queryset = Asset.objects.filter(
                Q(project_id=project) | Q(project__isnull=True)
            )
        else:
            item_fields["asset"].queryset = Asset.objects.none()
        return fields

    def validate_project(self, value):
        if value is None:
            raise serializers.ValidationError("An order must belong to a project.")
        return value

    def _resolution(self, payment):
        cached = getattr(payment, "_resolution", None)
        if cached is None:
            cached = resolve_payment(payment)
            payment._resolution = cached
        return cached

    def get_items_total_settled(self, obj) -> str:
        return str(self._resolution(obj).allocated)

    def get_variance(self, obj) -> str:
        return str(self._resolution(obj).variance)

    def get_is_balanced(self, obj) -> bool:
        return self._resolution(obj).is_balanced

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # `splits` is declared for WRITING; on read, build it from the real
        # child rows so the client gets back exactly the tree it sent.
        data["splits"] = [
            {
                "id": purchase.id,
                "receipt_number": purchase.receipt_number,
                "date": purchase.date.isoformat() if purchase.date else None,
                "currency": purchase.currency,
                "shipping": str(purchase.shipping),
                "tax": str(purchase.tax),
                "total": str(purchase.total),
                "settled_amount": (
                    None if purchase.settled_amount is None else str(purchase.settled_amount)
                ),
                "attachments": PurchaseAttachmentSerializer(
                    purchase.attachments.all(), many=True
                ).data,
                "items": [
                    {
                        "id": item.id,
                        "description": item.description,
                        "amount": str(item.amount),
                        "category": item.category_id,
                        "asset": next((link.asset_id for link in item.asset_links.all()), None),
                    }
                    for item in purchase.expenses.all()
                ],
            }
            for purchase in instance.purchases.all()
        ]
        return data

    # --- writes ----------------------------------------------------------

    def _write_splits(self, order: Payment, splits: list[dict]) -> None:
        """Replace the order's splits and their items with `splits`.

        Set semantics, like every other nested write in this codebase: children
        not present in the payload are removed. Items are `projects.Expense`
        rows, so removing one removes a real cost from the project's ledger —
        which is correct, because the user just deleted it from the order.

        A split with no explicit `total` takes the sum of its own items, which
        is what the user means when they type item prices and never touch a
        receipt subtotal.
        """
        keep_split_ids = [split["id"] for split in splits if split.get("id")]
        Purchase.objects.filter(payment=order).exclude(id__in=keep_split_ids).delete()

        for split in splits:
            items = split.get("items") or []
            items_total = sum((item["amount"] for item in items), Decimal("0"))
            total = split.get("total")
            if total is None:
                total = (
                    items_total
                    + (split.get("shipping") or Decimal("0"))
                    + (split.get("tax") or Decimal("0"))
                )

            values = {
                "vendor": order.vendor,
                "receipt_number": split.get("receipt_number", ""),
                "date": split.get("date") or order.paid_on,
                # A split with no currency of its own is priced in the same
                # currency the bank charged — the ordinary domestic case, where
                # the user should never have to think about currency at all.
                "currency": (split.get("currency") or order.currency).upper(),
                "shipping": split.get("shipping") or Decimal("0"),
                "tax": split.get("tax") or Decimal("0"),
                "total": total,
                "settled_amount": split.get("settled_amount"),
            }

            if split.get("id"):
                purchase = Purchase.objects.get(pk=split["id"], payment=order)
                for field, value in values.items():
                    setattr(purchase, field, value)
                purchase.save()
            else:
                purchase = Purchase.objects.create(
                    tenant_id=order.tenant_id, payment=order, **values
                )

            keep_item_ids = [item["id"] for item in items if item.get("id")]
            Expense.objects.filter(purchase=purchase).exclude(id__in=keep_item_ids).delete()

            for item in items:
                item_values = {
                    "project_id": order.project_id,
                    "purchase": purchase,
                    "category": item.get("category"),
                    "amount": item["amount"],
                    "currency": purchase.currency,
                    "date": purchase.date,
                    "vendor": order.vendor,
                    "description": item.get("description", ""),
                }
                if item.get("id"):
                    expense = Expense.objects.get(pk=item["id"], purchase=purchase)
                    for field, value in item_values.items():
                        setattr(expense, field, value)
                    expense.save()
                else:
                    expense = Expense.objects.create(
                        tenant_id=order.tenant_id,
                        created_by=self.context["request"].user,
                        **item_values,
                    )
                # Asset links reuse the Phase 1 service, so the per-asset cost
                # split behaves identically whether an item was entered here or
                # through the standalone expense form.
                if "asset" in item:
                    asset = item["asset"]
                    sync_expense_asset_links(
                        expense, [(asset, None, 1)] if asset is not None else []
                    )

    @transaction.atomic
    def create(self, validated_data):
        splits = validated_data.pop("splits", [])
        order = Payment.objects.create(**validated_data)
        # An order always has at least one split, even when the user never saw
        # the concept — see this module's docstring.
        self._write_splits(order, splits or [{"items": []}])
        return order

    @transaction.atomic
    def update(self, instance, validated_data):
        splits = validated_data.pop("splits", None)
        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save()
        if splits is not None:
            self._write_splits(instance, splits or [{"items": []}])
        return instance


class OrderViewSet(viewsets.ModelViewSet):
    """`/api/v1/orders/` — the one endpoint the expense UI talks to.

    `?project=` scopes the list to a project, which is how the project hub's
    Expenses tab shows a project its own orders.
    """

    serializer_class = OrderSerializer
    permission_classes = [PaymentPermission]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_queryset(self):
        queryset = (
            Payment.objects.select_related("project", "created_by")
            .prefetch_related(
                "attachments",
                "purchases__attachments",
                "purchases__expenses__asset_links",
            )
            .annotate(number=order_number_expr())
        )
        project_id = self.request.query_params.get("project")
        if project_id:
            queryset = queryset.filter(project_id=project_id)
        return queryset

    def perform_create(self, serializer):
        order = serializer.save(
            tenant=self.request.user.tenant,  # type: ignore[union-attr]
            created_by=self.request.user,
        )
        write_audit_log(
            tenant_id=order.tenant_id,
            actor=self.request.user,
            action=PAYMENT_MANAGE,
            entity_type="order",
            entity_id=order.id,
            before=None,
            after=OrderSerializer(order, context=self.get_serializer_context()).data,
            ip=client_ip(self.request),
        )

    def perform_update(self, serializer):
        context = self.get_serializer_context()
        before = OrderSerializer(serializer.instance, context=context).data
        order = serializer.save()
        write_audit_log(
            tenant_id=order.tenant_id,
            actor=self.request.user,
            action=PAYMENT_MANAGE,
            entity_type="order",
            entity_id=order.id,
            before=before,
            after=OrderSerializer(order, context=context).data,
            ip=client_ip(self.request),
        )

    @action(detail=True, methods=["post"], url_path="pack")
    def pack(self, request, pk=None):
        """`POST /api/v1/orders/{id}/pack/` — enqueue the expense pack PDF.

        Same async-job contract as every other PDF in this codebase: create the
        `Job` inside this request's transaction, dispatch on commit so the task
        cannot race a not-yet-committed row, and hand the client a job to poll
        at `GET /api/v1/jobs/{id}` until `download_url` appears.

        Rendering is never done in the request cycle — a pack merges every
        receipt scan and asset photo, which is exactly the kind of work that
        must not hold a gunicorn worker.
        """
        order = self.get_object()
        job = Job.objects.create(
            tenant=request.user.tenant,
            job_type="expense_pack_pdf",
            params={"order_id": order.id},
            created_by=request.user,
        )
        # A financial-document export is worth an immutable trail even though
        # it mutates nothing — same posture as the M7 project report.
        write_audit_log(
            tenant_id=order.tenant_id,
            actor=request.user,
            action=PAYMENT_MANAGE,
            entity_type="expense_pack",
            entity_id=order.id,
            before=None,
            after={"job_id": str(job.id)},
            ip=client_ip(request),
        )
        transaction.on_commit(
            lambda: generate_order_pack_pdf.delay(
                job_id=str(job.id), tenant_id=job.tenant_id, order_id=order.id
            )
        )
        return Response(JobSerializer(job).data, status=status.HTTP_202_ACCEPTED)

    def perform_destroy(self, instance):
        before = OrderSerializer(instance, context=self.get_serializer_context()).data
        tenant_id, order_id = instance.tenant_id, instance.id
        instance.delete()
        write_audit_log(
            tenant_id=tenant_id,
            actor=self.request.user,
            action=PAYMENT_MANAGE,
            entity_type="order",
            entity_id=order_id,
            before=before,
            after=None,
            ip=client_ip(self.request),
        )
