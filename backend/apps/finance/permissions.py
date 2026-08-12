"""RBAC for the money layer (M8 Phase 2, `docs/tasks/M8-expense-reconciliation.md` §7).

**This module is the whole reason `Payment`/`Purchase` needed thinking about.**
Every other tenant table in this codebase answers "which project is this?" with
a column, so the union-of-memberships scope rule and the RLS policy can both be
applied mechanically. A bank charge has no such column — one Amazon debit
legitimately pays for two grants at once — so "can this lead see it" is not a
property of the row, it is a property of the row's *children*.

Hence the **visible set**:

    a lead may see a charge iff at least one expense line under it is booked
    to a project they lead.

with two consequences that the tests pin:

- Charges touching only other people's projects are invisible — not 403,
  simply absent, exactly like any other tenant-scoped row they cannot reach.
- On a charge they CAN see, other projects' lines are never itemized. They
  collapse to a single unnamed total (`apps.finance.services.
  project_share_of_payment`), so a lead learns "the rest of this charge went
  elsewhere" but never which project or what was bought.

**The accepted residual, agreed with the user (§10.5):** the charge's own
header total is visible to anyone who can see the charge at all. SAR 4,631.10
was one debit; a lead reconciling their own share can see that figure and infer
some of it went elsewhere. Hiding it would make reconciliation impossible,
which is the entire purpose of the feature. What they can never see is where.

Because this rule lives here and not in the DB, `finance_payment`'s RLS policy
is tenant-only (see `apps.finance.migrations.0001_initial`) — the one table
pair in this codebase whose DB policy is broader than its application rule.
That is why the RBAC tests here are load-bearing rather than belt-and-braces.
"""

from __future__ import annotations

from rest_framework.permissions import SAFE_METHODS, BasePermission

from apps.rbac.permission_keys import PAYMENT_MANAGE, PAYMENT_VIEW
from apps.rbac.services import (
    get_viewable_project_scope,
    user_has_permission_in_any_scope,
)


def finance_scope(user) -> tuple[bool, set[int]]:
    """`(tenant_wide, project_ids)` for this user's `finance.payment.view`.

    Resolved ONCE per request and reused for every row — the same precomputed
    -scope technique `apps.projects.serializers.ProjectSerializer` uses to
    avoid a per-row RBAC query on a list.
    """
    tenant_wide, project_ids = get_viewable_project_scope(user, PAYMENT_VIEW)
    return tenant_wide, set(project_ids or [])


def visible_payments(queryset, user):
    """Narrow a `Payment` queryset to the caller's visible set (above).

    A tenant-wide holder (Admin) sees everything. Everyone else sees charges
    with at least one line in a project they lead. `.distinct()` because the
    join multiplies a charge by its matching lines.
    """
    tenant_wide, project_ids = finance_scope(user)
    if tenant_wide:
        return queryset
    if not project_ids:
        return queryset.none()
    return queryset.filter(purchases__expenses__project_id__in=project_ids).distinct()


def visible_purchases(queryset, user):
    """The same rule one level down, for `Purchase`."""
    tenant_wide, project_ids = finance_scope(user)
    if tenant_wide:
        return queryset
    if not project_ids:
        return queryset.none()
    return queryset.filter(expenses__project_id__in=project_ids).distinct()


def _covers_every_line(user, payment) -> bool:
    """True when every line under `payment` belongs to a project the caller
    leads (or they hold the key tenant-wide).

    Gates **header** edits: a charge whose lines span two leads' projects must
    not be editable by either of them, or they would silently overwrite each
    other's shared record. Their own lines stay fully editable regardless —
    those are `projects.Expense` rows, gated by `expense.manage` as always.
    """
    tenant_wide, project_ids = finance_scope(user)
    if tenant_wide:
        return True
    line_projects = {
        expense.project_id
        for purchase in payment.purchases.all()
        for expense in purchase.expenses.all()
    }
    # A charge with no lines yet is fully covered by whoever is recording it —
    # otherwise a lead could create a charge and immediately be locked out of
    # correcting a typo in it.
    return line_projects <= project_ids


class PaymentPermission(BasePermission):
    """`apps.finance.api.PaymentViewSet`.

    Two-phase, like every other permission class here: a permissive "holds the
    key somewhere" gate before `get_object()` runs (denying outright would
    re-introduce the M0 over-deny bug for a pure ProjectLead, whose only
    membership is project-scoped), then the real decision once the object is
    known. Row-level visibility is separately enforced by `visible_payments`
    in the viewset's queryset — an invisible charge 404s rather than 403s.
    """

    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False
        key = PAYMENT_VIEW if request.method in SAFE_METHODS else PAYMENT_MANAGE
        return user_has_permission_in_any_scope(user, key)

    def has_object_permission(self, request, view, obj) -> bool:
        user = request.user
        if request.method in SAFE_METHODS:
            # Reaching the object at all means it survived `visible_payments`.
            return user_has_permission_in_any_scope(user, PAYMENT_VIEW)
        if not user_has_permission_in_any_scope(user, PAYMENT_MANAGE):
            return False
        return _covers_every_line(user, obj)


class PurchasePermission(BasePermission):
    """`apps.finance.api.PurchaseViewSet` — same rule one level down.

    Header edits require covering every line ON THAT RECEIPT (not the whole
    charge): a receipt is a finer-grained record, and a lead who owns all of
    one receipt's items should be able to correct its total or attach its scan
    even when a sibling receipt on the same charge belongs to someone else.
    """

    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False
        key = PAYMENT_VIEW if request.method in SAFE_METHODS else PAYMENT_MANAGE
        return user_has_permission_in_any_scope(user, key)

    def has_object_permission(self, request, view, obj) -> bool:
        user = request.user
        if request.method in SAFE_METHODS:
            return user_has_permission_in_any_scope(user, PAYMENT_VIEW)
        if not user_has_permission_in_any_scope(user, PAYMENT_MANAGE):
            return False
        tenant_wide, project_ids = finance_scope(user)
        if tenant_wide:
            return True
        return {e.project_id for e in obj.expenses.all()} <= project_ids
