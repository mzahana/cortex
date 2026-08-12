"""Endpoints for the money layer (M8 Phase 2).

- `/api/v1/payments/` — bank charges, with their receipts nested in the
  payload and the reconciliation tally computed per row.
- `/api/v1/purchases/` — vendor receipts, creatable/editable on their own so a
  receipt can be logged before it is matched to a charge.
- `POST /payments/{id}/attachment/` and `POST /purchases/{id}/attachment/` —
  the statement excerpt and the receipt scan.

Tenant scoping (golden path step 2): every `get_queryset` builds from the
tenant-scoped manager fresh per request. **Then** `apps.finance.permissions.
visible_payments`/`visible_purchases` narrows further to the caller's visible
set — the project-level rule the DB cannot express for these tables (see that
module, and the RLS note in `migrations/0001_initial`).

Audit (step 5): every mutation writes a before/after entry, since creating or
editing a bank charge is exactly the kind of financial change `docs/rbac.md`
§5 exists to record.
"""

from __future__ import annotations

import logging

import django_filters as filters
from django.core.files.storage import default_storage
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from apps.assets.services import (
    PHOTO_CONTENT_TYPES,
    save_attachment_file,
    validate_attachment_upload,
)
from apps.audit.services import client_ip, write_audit_log
from apps.common.errors import problem_response
from apps.rbac.permission_keys import PAYMENT_MANAGE

from .models import Payment, PaymentAttachment, Purchase, PurchaseAttachment
from .permissions import PaymentPermission, PurchasePermission, visible_payments, visible_purchases
from .serializers import (
    PaymentAttachmentSerializer,
    PaymentSerializer,
    PurchaseAttachmentSerializer,
    PurchaseSerializer,
)

logger = logging.getLogger(__name__)


def _upload_kind(uploaded_file) -> str:
    """`"photo"` for an image, `"doc"` for everything else.

    Paperwork arrives both ways — a PDF from the vendor's email, or a phone
    photo of a paper receipt — and the user should not have to tell the app
    which they picked. The two `kind` allowlists are disjoint
    (`apps.assets.services`: `photo` is images only, `doc` is PDF/office/text),
    so hardcoding either one rejects half of what people legitimately upload.
    Deriving it from the file's own content type accepts both while keeping the
    same conservative default-deny validation.
    """
    content_type = (getattr(uploaded_file, "content_type", "") or "").lower()
    return "photo" if content_type in PHOTO_CONTENT_TYPES else "doc"


class PaymentFilterSet(filters.FilterSet):
    """`?vendor=`, `?paid_on_after=`/`?paid_on_before=`, `?unreconciled=`.

    `unreconciled` is the reconciliation screen's whole purpose — "which
    charges still have money I haven't accounted for" — but it cannot be a DB
    filter, because the variance is derived, not stored. It is applied in
    `PaymentViewSet.filter_queryset` after pagination-free resolution instead;
    declared here only so django-filter does not reject the query param.
    """

    vendor = filters.CharFilter(lookup_expr="icontains")
    paid_on_after = filters.DateFilter(field_name="paid_on", lookup_expr="gte")
    paid_on_before = filters.DateFilter(field_name="paid_on", lookup_expr="lte")
    # `?project=` — charges that paid for ANY item booked to this project.
    # This is how the project hub shows a project its own charges without a
    # separate top-level screen. A charge spanning two projects legitimately
    # appears under BOTH: it is one debit, and each project needs to see it to
    # reconcile its own share (the other project's receipts are still filtered
    # out of the payload and its total still collapses to one unnamed figure).
    project = filters.NumberFilter(field_name="purchases__expenses__project_id", distinct=True)

    class Meta:
        model = Payment
        fields = ["vendor", "paid_on_after", "paid_on_before", "project"]


def _payment_queryset():
    """Tenant-scoped, with the full child tree prefetched.

    The reconciliation figures need every purchase and every line under the
    charge, so a naive serialization is N+1 per row. One prefetch chain keeps a
    page of charges to a bounded number of queries regardless of page size.
    """
    return Payment.objects.select_related("created_by").prefetch_related(
        "attachments",
        "purchases__attachments",
        "purchases__expenses",
        "purchases__payment",
    )


class PaymentViewSet(viewsets.ModelViewSet):
    """`/api/v1/payments/` — one row per line on the bank statement."""

    serializer_class = PaymentSerializer
    permission_classes = [PaymentPermission]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    filter_backends = [DjangoFilterBackend]
    filterset_class = PaymentFilterSet

    def get_queryset(self):
        return visible_payments(_payment_queryset(), self.request.user)

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)
        if self.request.query_params.get("unreconciled") in ("true", "1"):
            # Derived, so it cannot be a WHERE clause — see `PaymentFilterSet`.
            unbalanced = [
                payment.id
                for payment in queryset
                if not PaymentSerializer(payment, context=self.get_serializer_context()).data[
                    "is_balanced"
                ]
            ]
            queryset = queryset.filter(id__in=unbalanced)
        return queryset

    def perform_create(self, serializer):
        payment = serializer.save(
            tenant=self.request.user.tenant,  # type: ignore[union-attr]
            created_by=self.request.user,
        )
        write_audit_log(
            tenant_id=payment.tenant_id,
            actor=self.request.user,
            action=PAYMENT_MANAGE,
            entity_type="payment",
            entity_id=payment.id,
            before=None,
            after=PaymentSerializer(payment, context=self.get_serializer_context()).data,
            ip=client_ip(self.request),
        )

    def perform_update(self, serializer):
        context = self.get_serializer_context()
        before = PaymentSerializer(serializer.instance, context=context).data
        payment = serializer.save()
        write_audit_log(
            tenant_id=payment.tenant_id,
            actor=self.request.user,
            action=PAYMENT_MANAGE,
            entity_type="payment",
            entity_id=payment.id,
            before=before,
            after=PaymentSerializer(payment, context=context).data,
            ip=client_ip(self.request),
        )

    def perform_destroy(self, instance):
        before = PaymentSerializer(instance, context=self.get_serializer_context()).data
        tenant_id, payment_id = instance.tenant_id, instance.id
        instance.delete()
        write_audit_log(
            tenant_id=tenant_id,
            actor=self.request.user,
            action=PAYMENT_MANAGE,
            entity_type="payment",
            entity_id=payment_id,
            before=before,
            after=None,
            ip=client_ip(self.request),
        )

    @action(
        detail=True,
        methods=["post"],
        url_path="attachment",
        parser_classes=[MultiPartParser, FormParser],
    )
    def attachment(self, request, pk=None):
        """`POST /payments/{id}/attachment/` — the bank statement excerpt."""
        payment = self.get_object()
        upload = request.FILES.get("file")
        if upload is None:
            return problem_response(
                status_code=status.HTTP_400_BAD_REQUEST,
                title="Invalid upload",
                detail="No file was provided under the 'file' field.",
            )

        # A statement excerpt is a document, not a photo — `kind="doc"` selects
        # the PDF/image-scan allowlist. Raises `ValidationError` (-> RFC-7807
        # 400) before a single byte reaches storage.
        validate_attachment_upload(kind=_upload_kind(upload), uploaded_file=upload)
        storage_key, content_type, size = save_attachment_file(
            tenant_id=payment.tenant_id,
            anchor_id=payment.id,
            uploaded_file=upload,
            prefix="payment-attachments",
        )
        attachment = PaymentAttachment.objects.create(
            tenant_id=payment.tenant_id,
            payment=payment,
            storage_key=storage_key,
            filename=upload.name,
            content_type=content_type,
            size=size,
            uploaded_by=request.user,
        )
        return Response(
            PaymentAttachmentSerializer(attachment).data, status=status.HTTP_201_CREATED
        )


class PurchaseFilterSet(filters.FilterSet):
    payment = filters.NumberFilter(field_name="payment_id")
    vendor = filters.CharFilter(lookup_expr="icontains")
    # Receipts carrying at least one item booked to this project.
    project = filters.NumberFilter(field_name="expenses__project_id", distinct=True)
    # `?unlinked=true` — receipts not yet matched to a bank charge. A normal,
    # expected state, and the other half of the reconciliation to-do list.
    unlinked = filters.BooleanFilter(field_name="payment_id", lookup_expr="isnull")

    class Meta:
        model = Purchase
        fields = ["payment", "vendor", "unlinked", "project"]


class PurchaseViewSet(viewsets.ModelViewSet):
    """`/api/v1/purchases/` — one row per vendor receipt / split shipment."""

    serializer_class = PurchaseSerializer
    permission_classes = [PurchasePermission]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    filter_backends = [DjangoFilterBackend]
    filterset_class = PurchaseFilterSet

    def get_queryset(self):
        return visible_purchases(
            Purchase.objects.select_related("payment", "created_by").prefetch_related(
                "attachments", "expenses", "payment__purchases__expenses"
            ),
            self.request.user,
        )

    def perform_create(self, serializer):
        purchase = serializer.save(
            tenant=self.request.user.tenant,  # type: ignore[union-attr]
            created_by=self.request.user,
        )
        write_audit_log(
            tenant_id=purchase.tenant_id,
            actor=self.request.user,
            action=PAYMENT_MANAGE,
            entity_type="purchase",
            entity_id=purchase.id,
            before=None,
            after=PurchaseSerializer(purchase, context=self.get_serializer_context()).data,
            ip=client_ip(self.request),
        )

    def perform_update(self, serializer):
        context = self.get_serializer_context()
        before = PurchaseSerializer(serializer.instance, context=context).data
        purchase = serializer.save()
        write_audit_log(
            tenant_id=purchase.tenant_id,
            actor=self.request.user,
            action=PAYMENT_MANAGE,
            entity_type="purchase",
            entity_id=purchase.id,
            before=before,
            after=PurchaseSerializer(purchase, context=context).data,
            ip=client_ip(self.request),
        )

    def perform_destroy(self, instance):
        before = PurchaseSerializer(instance, context=self.get_serializer_context()).data
        tenant_id, purchase_id = instance.tenant_id, instance.id
        # Expense lines survive (`Expense.purchase` is SET_NULL) — deleting a
        # mis-entered receipt must never destroy financial records.
        instance.delete()
        write_audit_log(
            tenant_id=tenant_id,
            actor=self.request.user,
            action=PAYMENT_MANAGE,
            entity_type="purchase",
            entity_id=purchase_id,
            before=before,
            after=None,
            ip=client_ip(self.request),
        )

    @action(
        detail=True,
        methods=["post"],
        url_path="attachment",
        parser_classes=[MultiPartParser, FormParser],
    )
    def attachment(self, request, pk=None):
        """`POST /purchases/{id}/attachment/` — the receipt scan.

        Filed against the RECEIPT, not its line items, so a report appendix
        prints it once rather than repeating it per item.
        """
        purchase = self.get_object()
        upload = request.FILES.get("file")
        if upload is None:
            return problem_response(
                status_code=status.HTTP_400_BAD_REQUEST,
                title="Invalid upload",
                detail="No file was provided under the 'file' field.",
            )

        validate_attachment_upload(kind=_upload_kind(upload), uploaded_file=upload)
        storage_key, content_type, size = save_attachment_file(
            tenant_id=purchase.tenant_id,
            anchor_id=purchase.id,
            uploaded_file=upload,
            prefix="purchase-attachments",
        )
        attachment = PurchaseAttachment.objects.create(
            tenant_id=purchase.tenant_id,
            purchase=purchase,
            storage_key=storage_key,
            filename=upload.name,
            content_type=content_type,
            size=size,
            uploaded_by=request.user,
        )
        return Response(
            PurchaseAttachmentSerializer(attachment).data, status=status.HTTP_201_CREATED
        )


class PurchaseAttachmentViewSet(mixins.DestroyModelMixin, viewsets.GenericViewSet):
    """`DELETE /api/v1/purchase-attachments/{id}/`."""

    serializer_class = PurchaseAttachmentSerializer
    permission_classes = [PurchasePermission]
    http_method_names = ["delete", "head", "options"]

    def get_queryset(self):
        return PurchaseAttachment.objects.select_related("purchase")

    def get_object(self):
        attachment = super().get_object()
        # Scope-check against the OWNING receipt, which is where the project
        # rule lives — the attachment row itself has no project.
        self.check_object_permissions(self.request, attachment.purchase)
        return attachment

    def perform_destroy(self, instance):
        tenant_id = instance.tenant_id
        attachment_id = instance.id
        storage_key = instance.storage_key
        before = {"filename": instance.filename, "storage_key": storage_key}
        instance.delete()

        # Delete the actual file too, not just the DB row: an orphaned blob
        # would keep consuming the volume forever with nothing pointing at it.
        # Best-effort by design — the DB delete is already committed, so a
        # missing file or a backend hiccup must not roll it back (same posture
        # as `apps.assets.api.AssetAttachmentViewSet`).
        try:
            default_storage.delete(storage_key)
        except Exception:
            logger.warning(
                "Failed to delete storage object %r for purchase_attachment %s (tenant %s) "
                "after DB row delete; DB delete already committed.",
                storage_key,
                attachment_id,
                tenant_id,
                exc_info=True,
            )

        write_audit_log(
            tenant_id=tenant_id,
            actor=self.request.user,
            action=PAYMENT_MANAGE,
            entity_type="purchase_attachment",
            entity_id=attachment_id,
            before=before,
            after=None,
            ip=client_ip(self.request),
        )


class PaymentAttachmentViewSet(mixins.DestroyModelMixin, viewsets.GenericViewSet):
    """`DELETE /api/v1/payment-attachments/{id}/`."""

    serializer_class = PaymentAttachmentSerializer
    permission_classes = [PaymentPermission]
    http_method_names = ["delete", "head", "options"]

    def get_queryset(self):
        return PaymentAttachment.objects.select_related("payment")

    def get_object(self):
        attachment = super().get_object()
        self.check_object_permissions(self.request, attachment.payment)
        return attachment

    def perform_destroy(self, instance):
        tenant_id = instance.tenant_id
        attachment_id = instance.id
        storage_key = instance.storage_key
        before = {"filename": instance.filename, "storage_key": storage_key}
        instance.delete()

        # Delete the actual file too, not just the DB row: an orphaned blob
        # would keep consuming the volume forever with nothing pointing at it.
        # Best-effort by design — the DB delete is already committed, so a
        # missing file or a backend hiccup must not roll it back (same posture
        # as `apps.assets.api.AssetAttachmentViewSet`).
        try:
            default_storage.delete(storage_key)
        except Exception:
            logger.warning(
                "Failed to delete storage object %r for payment_attachment %s (tenant %s) "
                "after DB row delete; DB delete already committed.",
                storage_key,
                attachment_id,
                tenant_id,
                exc_info=True,
            )

        write_audit_log(
            tenant_id=tenant_id,
            actor=self.request.user,
            action=PAYMENT_MANAGE,
            entity_type="payment_attachment",
            entity_id=attachment_id,
            before=before,
            after=None,
            ip=client_ip(self.request),
        )
