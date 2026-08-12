"""Reconciliation, FX and overhead allocation (M8 Phase 2 §2–§4).

Everything here is a **derivation over stored facts** — see the note in
`apps.finance.models` for why nothing computed is written back to a column.

Three properties this module guarantees, and which the tests pin:

1. A payment's receipts sum **exactly** to the payment.
2. A receipt's items (plus their share of shipping and tax) sum **exactly** to
   the receipt.
3. Every proportional split uses largest-remainder allocation, so no cent ever
   goes missing. A vanished cent is precisely the unexplained residual an
   auditor asks about.

## The FX rule (§3)

**The exchange rate is derived from the actual bank debit, never looked up.**
If a SAR 1,500.00 charge settles a single USD 400.00 receipt, the effective
rate is 3.75 — and that figure already contains the bank's FX markup and any
foreign-transaction fee. A published mid-market rate would leave a residual
that never balances, which defeats the entire purpose. No external rate source,
no scheduled sync, no new dependency.

Where one charge settles receipts in several different currencies the app
cannot infer the split, so the user types each receipt's settled amount off the
statement (`Purchase.settled_amount`) and the rate is derived per receipt.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, Sequence

ZERO = Decimal("0")
CENT = Decimal("0.01")


def allocate_proportionally(total: Decimal, weights: list[Decimal]) -> list[Decimal]:
    """Split `total` across `weights`, summing back to `total` EXACTLY.

    Largest-remainder allocation in integer cents: floor each share, then hand
    the leftover cents to the largest fractional remainders first. This is the
    one primitive behind every split in M8 — FX, shipping, tax, per-asset cost.

    All-zero (or empty) weights fall back to an even split, so a receipt whose
    items are all priced at 0.00 still distributes its shipping rather than
    silently dropping it.
    """
    count = len(weights)
    if count == 0:
        return []

    total_cents = int((total * 100).to_integral_value(rounding=ROUND_HALF_UP))
    weight_sum = sum(weights)

    if weight_sum == 0:
        base, remainder = divmod(abs(total_cents), count)
        sign = -1 if total_cents < 0 else 1
        return [Decimal(sign * (base + (1 if i < remainder else 0))) / 100 for i in range(count)]

    # Exact rational shares, then floor + distribute the remainder by size of
    # the fractional part (largest first, ties broken by original order so the
    # result is deterministic).
    exact = [Decimal(total_cents) * w / weight_sum for w in weights]
    floors = [int(e.to_integral_value(rounding="ROUND_FLOOR")) for e in exact]
    leftover = total_cents - sum(floors)

    order = sorted(range(count), key=lambda i: (-(exact[i] - floors[i]), i))
    for position in range(abs(leftover)):
        floors[order[position % count]] += 1 if leftover > 0 else -1

    return [Decimal(cents) / 100 for cents in floors]


@dataclass(frozen=True)
class LineAllocation:
    """One expense line's resolved position on its receipt."""

    expense_id: int
    project_id: int
    amount: Decimal  # as entered, transaction currency
    overhead: Decimal  # its share of shipping + tax, transaction currency
    loaded: Decimal  # amount + overhead — the fully-loaded cost
    settled: Decimal  # `loaded` converted to the payment's currency


@dataclass(frozen=True)
class PurchaseResolution:
    """A receipt's derived figures."""

    purchase_id: int
    currency: str
    total: Decimal
    overhead_total: Decimal  # shipping + tax
    items_total: Decimal  # sum of line amounts as entered
    variance: Decimal  # total - (items + overhead); 0 == balanced
    settled_amount: Decimal | None  # in the payment's currency; None = unsettled
    settlement_currency: str | None
    fx_rate: Decimal | None  # settled / total
    lines: list[LineAllocation]

    @property
    def is_balanced(self) -> bool:
        return self.variance == ZERO


@dataclass(frozen=True)
class PaymentResolution:
    """A bank charge's derived figures."""

    payment_id: int
    currency: str
    amount: Decimal
    allocated: Decimal  # sum of its receipts' settled amounts
    variance: Decimal  # amount - allocated; 0 == balanced
    purchases: list[PurchaseResolution]

    @property
    def is_balanced(self) -> bool:
        return self.variance == ZERO


def _settled_amounts(payment, purchases: list) -> dict[int, Decimal | None]:
    """Each purchase's cost in the payment's currency.

    Three cases, in order:

    1. **No payment** — nothing is settled yet; every value is None. Not an
       error, just a state ("receipt logged, charge not reconciled").
    2. **A purchase carries an explicit `settled_amount`** — trust it. This is
       the mixed-currency case, where only the statement knows the split.
    3. **The rest** share out whatever the payment has left, in proportion to
       their totals. When they all share one currency this reproduces a single
       effective FX rate; when they are already in the payment's currency the
       rate comes out at 1 and the user never sees any of this.

    Sharing the *remaining* amount (rather than the whole) is what lets cases 2
    and 3 coexist on one charge without double-counting.
    """
    if payment is None:
        return {p.id: None for p in purchases}

    resolved: dict[int, Decimal | None] = {}
    derived_targets = []

    for purchase in purchases:
        if purchase.settled_amount is not None:
            # Case 2: the statement is the only source of truth.
            resolved[purchase.id] = purchase.settled_amount
        elif (purchase.currency or "").upper() == (payment.currency or "").upper():
            # **Case 3a — no conversion needed, so no inference either.** The
            # receipt is already priced in the currency the bank charged, so it
            # contributes exactly its own total.
            #
            # This case is what makes an order's variance MEAN anything. An
            # earlier cut spread the payment across every receipt in proportion
            # to their totals, which made `sum(settled) == payment.amount` an
            # identity: variance was structurally always zero, so an order
            # missing half its items still reported "fully itemized" — in the
            # API, in the UI badge, and in the audit pack. Caught by
            # `test_unbalanced_order_says_so_rather_than_hiding_it`.
            resolved[purchase.id] = purchase.total
        else:
            derived_targets.append(purchase)

    # Case 3b: genuinely foreign-currency receipts with no explicit figure.
    # Only THESE share out what is left, because for them the rate is defined
    # by the debit — there is no independent number to compare against, and
    # pretending otherwise would invent a discrepancy that does not exist.
    accounted = sum((v for v in resolved.values() if v is not None), ZERO)
    remaining = payment.amount - accounted
    shares = allocate_proportionally(remaining, [p.total for p in derived_targets])
    for purchase, share in zip(derived_targets, shares, strict=True):
        resolved[purchase.id] = share

    return resolved


def resolve_purchase(
    purchase, expenses: Iterable, settled_amount: Decimal | None
) -> PurchaseResolution:
    """Derive one receipt's overhead spread, variance and per-line settled cost.

    Shipping and tax are spread across the lines **in proportion to their
    amounts** (M8 §4): a $350 GPU on a receipt with $30 shipping carries more
    of that shipping than a $9 cable does, which is what makes each line's
    fully-loaded cost — and therefore each asset's capitalized cost — correct.
    """
    lines = list(expenses)
    overhead_total = (purchase.shipping or ZERO) + (purchase.tax or ZERO)
    items_total = sum((line.amount or ZERO for line in lines), ZERO)

    overheads = allocate_proportionally(overhead_total, [line.amount or ZERO for line in lines])
    loaded = [
        (line.amount or ZERO) + overhead for line, overhead in zip(lines, overheads, strict=True)
    ]

    settled_each: Sequence[Decimal | None]
    if settled_amount is None:
        settled_each = [None] * len(lines)
        fx_rate = None
    else:
        # Convert by ALLOCATING the receipt's settled amount across the lines,
        # rather than multiplying each line by the rate independently. Independent
        # multiplication rounds per line and the results then fail to sum back to
        # the receipt — the reconciliation would be off by a cent or two for no
        # visible reason.
        settled_each = allocate_proportionally(settled_amount, loaded)
        fx_rate = (
            (settled_amount / purchase.total).quantize(Decimal("0.00000001"))
            if purchase.total
            else None
        )

    return PurchaseResolution(
        purchase_id=purchase.id,
        currency=purchase.currency,
        total=purchase.total,
        overhead_total=overhead_total,
        items_total=items_total,
        variance=purchase.total - (items_total + overhead_total),
        settled_amount=settled_amount,
        settlement_currency=(purchase.payment.currency if purchase.payment_id else None),
        fx_rate=fx_rate,
        lines=[
            LineAllocation(
                expense_id=line.id,
                project_id=line.project_id,
                amount=line.amount or ZERO,
                overhead=overhead,
                loaded=loaded_amount,
                settled=settled if settled is not None else ZERO,
            )
            for line, overhead, loaded_amount, settled in zip(
                lines, overheads, loaded, settled_each, strict=True
            )
        ],
    )


def resolve_payment(payment) -> PaymentResolution:
    """Derive a whole charge: its receipts, their settled amounts, the variance.

    One prefetched pass — `purchases__expenses` — so this stays cheap enough to
    call per row on the reconciliation list.
    """
    purchases = list(payment.purchases.all())
    settled = _settled_amounts(payment, purchases)

    resolutions = [
        resolve_purchase(purchase, purchase.expenses.all(), settled.get(purchase.id))
        for purchase in purchases
    ]
    allocated = sum((r.settled_amount or ZERO for r in resolutions), ZERO)

    return PaymentResolution(
        payment_id=payment.id,
        currency=payment.currency,
        amount=payment.amount,
        allocated=allocated,
        variance=payment.amount - allocated,
        purchases=resolutions,
    )


def project_share_of_payment(
    resolution: PaymentResolution, project_ids: set[int]
) -> tuple[Decimal, Decimal]:
    """`(this project's share, everything else)` of a charge, in its currency.

    Powers both the report's reconciliation appendix and the RBAC redaction: a
    lead who can see a shared charge sees their own share itemized and the rest
    as a single unnamed total, never another project's detail.
    """
    mine = ZERO
    theirs = ZERO
    for purchase in resolution.purchases:
        for line in purchase.lines:
            if line.project_id in project_ids:
                mine += line.settled
            else:
                theirs += line.settled
    return mine, theirs


# --- Expense pack (M8 Phase 3 §6.1) ----------------------------------------
#
# The ONLY place in the finance app that reads the database or the storage
# backend for the pack. `apps.finance.pack` receives frozen dataclasses and
# never touches an ORM instance — same separation `apps.projects.services` /
# `apps.projects.report` keep, so the renderer stays testable without a DB and
# can never trigger a lazy query mid-render.


def _money(amount, currency: str) -> str:
    if amount is None:
        return ""
    return f"{currency} {Decimal(amount):,.2f}".strip()


def resolve_order_pack_data(order, *, generated_by: str = ""):
    """Turn one order into `apps.finance.pack.OrderPackData`.

    Must be called inside `tenant_context` — it reads tenant-scoped rows and
    the storage backend, exactly like `resolve_project_report_data`.
    """
    from django.utils import timezone

    from apps.projects.report import DocumentFile
    from apps.projects.services import _asset_photo_data_uri

    from .pack import OrderPackData, PackItem, PackShipment

    resolution = resolve_payment(order)
    by_purchase = {r.purchase_id: r for r in resolution.purchases}

    purchases = list(order.purchases.all())
    shipments = []
    scan_files: list[DocumentFile] = []

    # The statement first: it is the anchor an auditor matches everything else
    # against, so it should be the first thing after the typeset pages.
    for attachment in order.attachments.all():
        raw = _read_storage_bytes(attachment.storage_key)
        if raw is not None:
            scan_files.append(
                DocumentFile(
                    label=f"Bank statement — {attachment.filename}",
                    content_type=attachment.content_type or "",
                    raw_bytes=raw,
                )
            )

    for index, purchase in enumerate(purchases, start=1):
        detail = by_purchase.get(purchase.id)
        label = f"Shipment {index} of {len(purchases)}" if len(purchases) > 1 else "Items"

        settled_by_expense = (
            {line.expense_id: line.settled for line in detail.lines} if detail else {}
        )
        # Narrow once, explicitly: `detail` is None only if `resolve_payment`
        # somehow omitted this purchase, and every branch below reads through
        # it. A local flag keeps both the runtime guard and the type checker
        # honest instead of repeating `detail is not None` five times.
        settlement_currency = detail.settlement_currency if detail else None
        settled_total = detail.settled_amount if detail else None
        fx_rate = detail.fx_rate if detail else None
        converted = bool(
            settled_total is not None
            and settlement_currency
            and settlement_currency != purchase.currency
        )

        items = []
        for expense in purchase.expenses.all():
            asset = next(
                (link.asset for link in expense.asset_links.all()),
                None,
            )
            items.append(
                PackItem(
                    description=expense.description or expense.vendor or "",
                    amount=_money(expense.amount, purchase.currency),
                    settled=(
                        _money(settled_by_expense.get(expense.id), settlement_currency or "")
                        if converted
                        else ""
                    ),
                    asset_name=asset.name if asset is not None else "",
                    photo_data_uri=(_asset_photo_data_uri(asset) if asset is not None else None),
                )
            )

        overhead = (purchase.shipping or ZERO) + (purchase.tax or ZERO)
        shipments.append(
            PackShipment(
                label=label,
                receipt_number=purchase.receipt_number,
                date=purchase.date.isoformat() if purchase.date else "",
                total=_money(purchase.total, purchase.currency),
                settled=(_money(settled_total, settlement_currency or "") if converted else ""),
                # Original first, converted in parentheses with the rate — the
                # auditor is holding the USD receipt, not the SAR statement.
                fx_note=(
                    f"{_money(purchase.total, purchase.currency)} "
                    f"({_money(settled_total, settlement_currency or '')} @ {fx_rate})"
                    if converted
                    else _money(purchase.total, purchase.currency)
                ),
                items_subtotal=_money(detail.items_total if detail else ZERO, purchase.currency),
                shipping=_money(purchase.shipping or ZERO, purchase.currency),
                tax=_money(purchase.tax or ZERO, purchase.currency),
                has_overhead=bool(overhead),
                items=items,
            )
        )

        for attachment in purchase.attachments.all():
            raw = _read_storage_bytes(attachment.storage_key)
            if raw is not None:
                scan_files.append(
                    DocumentFile(
                        label=f"{label} receipt — {attachment.filename}",
                        content_type=attachment.content_type or "",
                        raw_bytes=raw,
                    )
                )

    variance = resolution.variance
    return OrderPackData(
        tenant_name=order.tenant.name,
        project_name=order.project.name if order.project_id else "",
        vendor=order.vendor,
        paid_on=order.paid_on.isoformat() if order.paid_on else "",
        amount=_money(order.amount, order.currency),
        account_label=order.account_label,
        statement_ref=order.statement_ref,
        shipments=shipments,
        items_total=_money(resolution.allocated, order.currency),
        variance_note=(
            "0.00 — fully itemized"
            if variance == ZERO
            else _money(variance, order.currency) + " unaccounted for"
        ),
        is_balanced=resolution.is_balanced,
        generated_note=(
            f"Generated {timezone.now().date().isoformat()}"
            + (f" by {generated_by}" if generated_by else "")
        ),
        scan_files=scan_files,
    )


def _read_storage_bytes(storage_key: str) -> bytes | None:
    """Best-effort read. One missing scan must never fail a whole pack — the
    same posture every other document/photo helper in this codebase takes."""
    from django.core.files.storage import default_storage

    try:
        with default_storage.open(storage_key, "rb") as fh:
            return fh.read()
    except Exception:
        return None


def order_pack_storage_key(tenant_id: int, job_id) -> str:
    """Tenant-first, like every other key — see
    `apps.common.media` for why that layout is load-bearing."""
    return f"expense-packs/{tenant_id}/{job_id}.pdf"


def save_order_pack_pdf(*, tenant_id: int, job_id, pdf_bytes: bytes, order=None) -> tuple[str, str]:
    """Store the pack and return `(storage_key, download_filename)`.

    The storage key stays UUID-based (it must be unique and unguessable); only
    the DOWNLOAD name is human-readable. `order` is optional so existing
    callers/tests without one still work — they fall back to the old name.
    """
    import uuid as uuid_lib

    from django.core.files.base import ContentFile
    from django.core.files.storage import default_storage

    key = order_pack_storage_key(tenant_id, job_id)
    storage_key = default_storage.save(key, ContentFile(pdf_bytes))
    if order is not None:
        filename = order_pack_filename(order)
    else:
        filename = f"expense-pack-{uuid_lib.UUID(str(job_id)).hex[:8]}.pdf"
    return storage_key, filename


# --- Order numbering -------------------------------------------------------
#
# An order's number is its POSITION among its project's orders in statement
# order (`paid_on`, then `id`) — the same order the project report walks them
# in, so "Order 4" means the same thing in the report, in the UI and in a
# downloaded filename.
#
# Deliberately positional rather than a stored counter: it needs no migration
# and no write path, and it always matches the report. The tradeoff is real and
# worth knowing — **back-dating a new order renumbers the ones after it**. That
# is fine for reading a report (which is a snapshot) but means an order number
# is not a permanent identifier to quote in an email months later. If you need
# that, say so and it becomes a stored `order_no` assigned at creation.


def order_number_expr():
    """`RowNumber` window expression for an order's number.

    Annotating with a window function keeps a list of N orders at one query
    instead of N+1 — the numbering is computed by Postgres alongside the rows.
    """
    from django.db.models import F, Window
    from django.db.models.functions import RowNumber

    return Window(
        expression=RowNumber(),
        partition_by=[F("project_id")],
        order_by=[F("paid_on").asc(nulls_last=True), F("id").asc()],
    )


def order_number(order) -> int:
    """This one order's number, for callers holding a single instance (the
    Celery pack task). Uses the annotation when the queryset already carried
    it, otherwise counts the orders that sort before this one."""
    cached = getattr(order, "number", None)
    if cached:
        return int(cached)

    from django.db.models import Q

    from .models import Payment

    # `paid_on` is NOT NULL at the database level, so there is no undated case
    # to rank here — every order has a statement date to sort by.
    earlier = Payment.objects.filter(project_id=order.project_id).filter(
        Q(paid_on__lt=order.paid_on) | Q(paid_on=order.paid_on, id__lt=order.id)
    )
    return earlier.count() + 1


def order_pack_filename(order) -> str:
    """`order-04-aliexpress-2026-08-12.pdf` — the number first, so a folder of
    downloaded packs sorts into statement order and a pack can be matched to
    the report without opening it. The old name was a bare UUID fragment,
    which identified nothing."""
    from django.utils.text import slugify

    number = order_number(order)
    vendor = slugify(order.vendor or "")[:40] or "unknown-vendor"
    # `isoformat` rather than `str` for a real date, but an unsaved instance may
    # still be holding whatever the caller assigned — never let a filename be
    # the thing that raises.
    paid_on = order.paid_on
    dated = paid_on.isoformat() if hasattr(paid_on, "isoformat") else (str(paid_on or "undated"))
    return f"order-{number:02d}-{vendor}-{slugify(dated)}.pdf"


# --- Audit-readiness checklist (M8 Phase 3 §6.3) ---------------------------


def resolve_checklist_data(project, *, generated_by: str = ""):
    """Everything an auditor would ask about, found before they ask.

    Split into **blockers** (would actually fail an audit — money that is not
    accounted for, paperwork that does not exist) and **advisories** (untidy,
    but defensible). The distinction matters: a list that treats a missing
    serial number as equal to an unexplained SAR 400 is a list nobody reads.

    Must run inside `tenant_context`.
    """
    from django.utils import timezone

    from apps.assets.models import Asset

    from .checklist import ChecklistData, Finding
    from .models import Payment

    findings: list[Finding] = []

    orders = (
        Payment.objects.filter(project=project)
        .prefetch_related("attachments", "purchases__attachments", "purchases__expenses")
        .order_by("paid_on", "id")
    )

    for order in orders:
        where = f"{order.vendor or 'Expense'} · {order.paid_on} · {order.currency} {order.amount}"
        resolution = resolve_payment(order)

        if not resolution.is_balanced:
            findings.append(
                Finding(
                    severity="blocker",
                    what=f"{order.currency} {resolution.variance} of this charge is not itemized",
                    where=where,
                    fix="Add the missing items, or correct the amount paid.",
                )
            )

        if not order.attachments.all():
            findings.append(
                Finding(
                    severity="blocker",
                    what="No bank statement attached",
                    where=where,
                    fix="Attach the statement excerpt showing this deduction.",
                )
            )

        purchases = list(order.purchases.all())
        for index, purchase in enumerate(purchases, start=1):
            if not purchase.attachments.all():
                label = f"Shipment {index} of {len(purchases)}" if len(purchases) > 1 else "Receipt"
                findings.append(
                    Finding(
                        severity="blocker",
                        what=f"{label} has no receipt scan",
                        where=where,
                        fix="Attach the vendor receipt for this shipment.",
                    )
                )

            for expense in purchase.expenses.all():
                if expense.category_id is None:
                    findings.append(
                        Finding(
                            severity="advisory",
                            what=f"Item '{expense.description or 'untitled'}' has no category",
                            where=where,
                            fix="Set a category so it appears in the spend breakdown.",
                        )
                    )

    for asset in (
        Asset.objects.filter(project=project).prefetch_related("attachments").order_by("name")[:500]
    ):
        if not any(a.kind == "photo" for a in asset.attachments.all()):
            findings.append(
                Finding(
                    severity="advisory",
                    what="Asset has no photo",
                    where=asset.name,
                    fix="Add a photo so the item is identifiable in the audit pack.",
                )
            )
        if not asset.serial_number:
            findings.append(
                Finding(
                    severity="advisory",
                    what="Asset has no serial number",
                    where=asset.name,
                    fix="Record the serial number if the item has one.",
                )
            )

    return ChecklistData(
        tenant_name=project.tenant.name,
        project_name=project.name,
        generated_note=(
            f"Generated {timezone.now().date().isoformat()}"
            + (f" by {generated_by}" if generated_by else "")
        ),
        findings=findings,
    )


def checklist_storage_key(tenant_id: int, job_id) -> str:
    return f"audit-checklists/{tenant_id}/{job_id}.pdf"


def save_checklist_pdf(*, tenant_id: int, job_id, pdf_bytes: bytes) -> tuple[str, str]:
    import uuid as uuid_lib

    from django.core.files.base import ContentFile
    from django.core.files.storage import default_storage

    key = checklist_storage_key(tenant_id, job_id)
    storage_key = default_storage.save(key, ContentFile(pdf_bytes))
    filename = f"audit-readiness-{uuid_lib.UUID(str(job_id)).hex[:8]}.pdf"
    return storage_key, filename
