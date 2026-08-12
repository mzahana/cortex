"""The money layer (M8 Phase 2, `docs/tasks/M8-expense-reconciliation.md`).

M7's `projects.Expense` was doing three jobs at once — the money movement, the
receipt, and the line item — which only holds when 1 payment = 1 receipt =
1 item = 1 project. Real purchasing breaks that identity constantly: one bank
debit pays several vendor receipts, one receipt carries items for different
projects, and the receipt is priced in USD while the statement shows SAR.

So the three concepts get three records:

    Payment      one line on the bank statement  (SAR 4,631.10, Visa •4321)
      └ Purchase   one vendor receipt / shipment (USD 400.00, Amazon 123-456)
          └ Expense  one item, booked to ONE project  (projects.Expense)

**Why this app is not `apps.projects`.** `Payment` and `Purchase` span
projects by construction — a single Amazon charge can pay for two grants — so
they cannot be project-scoped the way every M7 table is. They are tenant-owned
but tenant-WIDE, which is a different RBAC and RLS shape (see
`apps.finance.permissions`), and mixing them into the project hub would blur
exactly the distinction this milestone exists to draw. `Expense` stays in
`apps.projects` and points here.

## What is stored vs. derived

Only *facts* are stored: what the bank took, what the receipt says, what each
item cost. Every reconciliation figure — each receipt's settled amount, its FX
rate, each line's share of shipping and tax, each line's cost in the
settlement currency — is **derived** by `apps.finance.services`, never written
to a column.

That is a deliberate departure from this milestone's own plan (§3 said "stored,
not computed"), and Phase 1 is why: a stored derived figure has no way to stay
correct when the thing it was derived from is edited, and no way to tell a
stale value from a deliberate one. Deriving keeps the arithmetic true by
construction — a payment's receipts always sum to the payment, a receipt's
items always sum to the receipt — which is the whole property an auditor is
checking. Reproducibility is preserved because the *inputs* are immutable
facts, and every derivation is largest-remainder allocation, which is
deterministic.

`Purchase.settled_amount` is the one apparent exception, and it is a stored
FACT, not a derivation: for a payment covering receipts in several different
currencies the app cannot infer each receipt's share, so the user reads it off
the statement and types it in.
"""

from __future__ import annotations

from django.db import models

from apps.tenancy.models import TenantScopedModel


class Payment(TenantScopedModel):
    """**An order**: one purchase from a vendor, paid by one bank deduction.

    This is the record the user thinks of as "an expense" — they place an
    order, the bank takes one amount, and the goods arrive in one or more
    shipments. `Purchase` rows are those shipments; `projects.Expense` rows are
    the individual items inside them.

    **An order belongs to exactly one project** (confirmed with the user): it
    is paid out of that project's funds, and its items buy that project's
    assets. An earlier cut modelled this as tenant-wide on the theory that one
    Amazon checkout might span two grants; the user's actual rule is stricter
    and simpler, and following it removes the cross-project sharing, the
    redaction, and the separate top-level screen that all existed only to
    serve that case.

    `amount`/`currency` are the **settlement** currency — what the bank
    actually took, in the account's own currency. This is the figure that must
    reconcile, and it is never derived.
    """

    class Method(models.TextChoices):
        CARD = "card", "Card"
        BANK_TRANSFER = "bank_transfer", "Bank transfer"
        CASH = "cash", "Cash"
        OTHER = "other", "Other"

    project = models.ForeignKey(
        "projects.Project",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="orders",
        help_text="The project whose funds paid for this order. Nullable only "
        "so the column could be added to rows that predate it; every order "
        "created through the API has one (the serializer requires it).",
    )
    amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        help_text="What the bank actually took, in the settlement currency.",
    )
    currency = models.CharField(
        max_length=3, help_text="Settlement currency, e.g. 'SAR'. What the statement shows."
    )
    paid_on = models.DateField()
    method = models.CharField(max_length=16, choices=Method.choices, default=Method.CARD)
    account_label = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Human label for the account/card, e.g. 'Visa •4321'. NEVER a full card number.",
    )
    statement_ref = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text="The bank's own reference for this line — the auditor's join key.",
    )
    vendor = models.CharField(max_length=255, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_payments",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "finance_payment"
        indexes = [
            models.Index(fields=["tenant", "paid_on"]),
            models.Index(fields=["tenant", "vendor"]),
            models.Index(fields=["tenant", "project"]),
        ]
        ordering = ["-paid_on", "-id"]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.currency} {self.amount} on {self.paid_on}"


class Purchase(TenantScopedModel):
    """One vendor receipt — or one shipment of a split order.

    This is the unit Amazon actually gives you: an order that arrives in three
    boxes produces three receipts, each with its own total, all settled by one
    charge. `payment` is nullable because a receipt often arrives before you
    have reconciled it against the statement; an unlinked receipt is a *state*
    (surfaced as "unsettled"), never an error.

    `subtotal`/`shipping`/`tax`/`total` are all in the **transaction**
    currency — what the receipt itself says. Shipping and tax are held at this
    level because that is where the vendor charges them; spreading them across
    the items is a derivation (`apps.finance.services.resolve_purchase`), not
    stored data.

    `settled_amount` is what this receipt cost in the PAYMENT's currency. Leave
    it null and the app derives it by sharing the payment out across its
    receipts — correct whenever they share one currency, which is the common
    case. Fill it in when one charge settles receipts in several different
    currencies, where no derivation is possible and the statement is the only
    source of truth.
    """

    payment = models.ForeignKey(
        Payment,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="purchases",
        help_text="NULL = not yet reconciled against a bank charge.",
    )
    vendor = models.CharField(max_length=255, blank=True, default="")
    vendor_order_number = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text="The vendor's order id. Deliberately NOT unique — split "
        "shipments of one order all share it.",
    )
    receipt_number = models.CharField(max_length=128, blank=True, default="")
    date = models.DateField()
    currency = models.CharField(
        max_length=3, help_text="Transaction currency — what the receipt is priced in, e.g. 'USD'."
    )
    subtotal = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    shipping = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    tax = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        help_text="The receipt's own grand total, in its own currency.",
    )
    settled_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="This receipt's cost in the PAYMENT's currency. NULL = derive "
        "it by sharing the payment across its receipts (see the model docstring).",
    )
    notes = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_purchases",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "finance_purchase"
        indexes = [
            models.Index(fields=["tenant", "payment"]),
            models.Index(fields=["tenant", "date"]),
            models.Index(fields=["tenant", "vendor"]),
        ]
        ordering = ["date", "id"]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.vendor} {self.currency} {self.total}"


class PaymentAttachment(TenantScopedModel):
    """The bank statement excerpt for a `Payment`.

    Same storage rule as every other attachment table in this codebase: the
    bytes live on the storage backend and only `storage_key` + metadata is
    written here, via `apps.assets.services.save_attachment_file` — the single
    writer of attachment bytes.
    """

    payment = models.ForeignKey(Payment, on_delete=models.CASCADE, related_name="attachments")
    storage_key = models.CharField(max_length=500)
    filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=127, blank=True, default="")
    size = models.PositiveBigIntegerField(default=0)
    uploaded_by = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="uploaded_payment_attachments",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "finance_payment_attachment"
        indexes = [models.Index(fields=["tenant", "payment"])]
        ordering = ["-created_at"]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.filename


class PurchaseAttachment(TenantScopedModel):
    """The receipt scan for a `Purchase`.

    **The scan belongs to the receipt, not to the line items on it.** That is
    what stops the same PDF being repeated once per item in a report appendix —
    the single ugliest thing about a hand-assembled audit pack, and the reason
    this table exists rather than reusing `projects.ExpenseAttachment`.
    """

    purchase = models.ForeignKey(Purchase, on_delete=models.CASCADE, related_name="attachments")
    storage_key = models.CharField(max_length=500)
    filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=127, blank=True, default="")
    size = models.PositiveBigIntegerField(default=0)
    uploaded_by = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="uploaded_purchase_attachments",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "finance_purchase_attachment"
        indexes = [models.Index(fields=["tenant", "purchase"])]
        ordering = ["-created_at"]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.filename
