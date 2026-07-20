"""Business logic for the reservation endpoints (T3.2): create (conflict +
per-user-cap + window checks, honoring `Category.requires_approval`),
approve, reject, cancel.

Kept out of `apps.reservations.api` (thin views, `CLAUDE.md`/`docs/rbac.md`
convention), same reasoning as `apps.stock.services`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.exceptions import APIException

from .models import Reservation
from .signals import approval_decision, approval_request, reservation_confirmed


class ReservationConflict(APIException):
    """`POST /reservations` -> `409 Conflict` when the requested window
    overlaps an existing ACTIVE reservation for the same asset (F4).

    Raised from TWO places, by design (task instructions):
    1. The app-layer pre-check in `create_reservation` below (the common
       case -- catches the conflict BEFORE ever hitting the DB, giving a
       precise, cheap 400-shaped-as-409 without relying on the DB erroring).
    2. As a translation of the raw `IntegrityError` the `0002` GiST
       EXCLUDE constraint (`reservation_no_overlap_active`) raises for the
       genuine concurrent-request race the pre-check's SELECT can't see
       (two requests reading "no conflict" in the same instant, both then
       inserting) -- caught in the `except IntegrityError` block below and
       re-raised as THIS exception so the client still gets the same clean,
       structured 409 either way, never a raw DB error string. (Without this
       translation the global `apps.common.errors.rfc7807_exception_handler`
       IntegrityError->409 fallback would still produce a 409, just with a
       generic "duplicate value" detail instead of this reservation-specific
       message -- this is the belt to that backstop's suspenders.)
    """

    status_code = status.HTTP_409_CONFLICT
    default_detail = "This asset is already reserved for an overlapping time window."
    default_code = "reservation_conflict"


@dataclass
class ReservationLimits:
    max_active_per_user: int
    max_lead_days: int
    max_duration_days: int


def get_reservation_limits() -> ReservationLimits:
    """The Q10 caps (`docs/tasks/M3-reservations-checkout.md`): **no
    confirmed product answer yet** -- these are documented defaults, read
    from Django settings (`config/settings/base.py`,
    `RESERVATION_MAX_ACTIVE_PER_USER` / `RESERVATION_MAX_LEAD_DAYS` /
    `RESERVATION_MAX_DURATION_DAYS`), the same 12-factor
    env-configurable-constant mechanism `MAX_ATTACHMENT_UPLOAD_BYTES` already
    uses in this codebase -- not a new per-tenant knob, see that setting's
    comment for why. Centralized here as the single place every caller
    (this service, any future admin-facing "current limits" endpoint) reads
    from, so a future per-tenant override only has to change this function.
    """
    return ReservationLimits(
        max_active_per_user=settings.RESERVATION_MAX_ACTIVE_PER_USER,
        max_lead_days=settings.RESERVATION_MAX_LEAD_DAYS,
        max_duration_days=settings.RESERVATION_MAX_DURATION_DAYS,
    )


def _check_conflict(*, asset, start_at, end_at, exclude_pk=None) -> None:
    """App-layer pre-check mirroring the DB's `0002` GiST EXCLUDE exactly:
    same `Reservation.ACTIVE_STATUSES` tuple, same `[start_at, end_at)`
    overlap test (`start_at < other.end_at AND end_at > other.start_at`).
    Single source of truth for BOTH halves of this invariant is
    `Reservation.ACTIVE_STATUSES` (see that model's docstring) -- this
    function must never diverge from it.
    """
    qs = Reservation.objects.filter(
        asset=asset,
        status__in=Reservation.ACTIVE_STATUSES,
        start_at__lt=end_at,
        end_at__gt=start_at,
    )
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    if qs.exists():
        raise ReservationConflict()


def create_reservation(
    *,
    tenant,
    actor,
    asset,
    start_at,
    end_at,
    project=None,
) -> Reservation:
    """Validate + create one `Reservation`.

    Order of checks (cheapest/most-specific first, so a caller gets the
    most useful error for what's actually wrong):
    1. Window sanity (lead time / max duration -- Q10 caps).
    2. Per-user active-reservation cap (Q10 cap).
    3. Conflict pre-check against `Reservation.ACTIVE_STATUSES` (F4).
    4. Insert, honoring `Category.requires_approval` (`docs/rbac.md` §4):
       `pending` if the asset's category requires approval, `approved`
       otherwise (self-service, immediately confirmed).

    The whole check-then-insert sequence runs inside one
    `transaction.atomic()` block so a concurrent request that slips past
    the pre-check (step 3) but loses the DB-level race is caught as an
    `IntegrityError` from the `0002` EXCLUDE constraint and translated to
    the SAME `ReservationConflict` 409 the pre-check would have raised --
    never a raw 500.
    """
    limits = get_reservation_limits()
    now = timezone.now()

    if start_at > now + timedelta(days=limits.max_lead_days):
        raise serializers.ValidationError(
            {
                "start_at": (
                    f"start_at cannot be more than {limits.max_lead_days} days " "in the future."
                )
            }
        )
    if (end_at - start_at) > timedelta(days=limits.max_duration_days):
        raise serializers.ValidationError(
            {"end_at": (f"A reservation cannot span more than {limits.max_duration_days} days.")}
        )

    active_count = Reservation.objects.filter(
        user=actor, status__in=Reservation.ACTIVE_STATUSES
    ).count()
    if active_count >= limits.max_active_per_user:
        raise serializers.ValidationError(
            {
                "non_field_errors": (
                    f"You already have {active_count} active reservation(s); the "
                    f"limit is {limits.max_active_per_user}."
                )
            }
        )

    _check_conflict(asset=asset, start_at=start_at, end_at=end_at)

    requires_approval = asset.category.requires_approval
    initial_status = (
        Reservation.Status.PENDING if requires_approval else Reservation.Status.APPROVED
    )

    try:
        with transaction.atomic():
            reservation = Reservation.objects.create(
                tenant=tenant,
                asset=asset,
                user=actor,
                project=project if project is not None else asset.project,
                start_at=start_at,
                end_at=end_at,
                status=initial_status,
            )
    except IntegrityError as exc:
        # The DB-level backstop firing (see `ReservationConflict` docstring,
        # case 2): the exclusion constraint's WHERE clause mirrors
        # `Reservation.ACTIVE_STATUSES` exactly, so any race the pre-check
        # above missed lands here, never as a raw 500.
        raise ReservationConflict() from exc

    if initial_status == Reservation.Status.APPROVED:
        _emit_reservation_confirmed_on_commit(reservation)
    else:
        _emit_approval_request_on_commit(reservation)

    return reservation


def approve_reservation(*, reservation: Reservation, actor, note: str = "") -> Reservation:
    """`reservation.approve` decision: `pending` -> `approved`. Re-checks the
    transition is actually valid (not just relying on the permission class
    having let the request through) -- same "service re-validates, not just
    the permission layer" posture as `apps.stock.services.
    apply_reorder_transition`.
    """
    if reservation.status != Reservation.Status.PENDING:
        raise serializers.ValidationError(
            {
                "status": (
                    f"Only a 'pending' reservation can be approved (this one is "
                    f"'{reservation.status}')."
                )
            }
        )
    reservation.status = Reservation.Status.APPROVED
    reservation.approver = actor
    reservation.approval_note = note
    reservation.save(update_fields=["status", "approver", "approval_note", "updated_at"])

    _emit_approval_decision_on_commit(reservation, approved=True, actor=actor, note=note)
    _emit_reservation_confirmed_on_commit(reservation)
    return reservation


def reject_reservation(*, reservation: Reservation, actor, note: str = "") -> Reservation:
    """`reservation.approve` decision: `pending` -> `rejected`. Frees the
    window immediately (a `rejected` reservation drops out of
    `Reservation.ACTIVE_STATUSES`, so the `0002` exclusion constraint no
    longer holds it, and the app-level conflict pre-check ignores it too)."""
    if reservation.status != Reservation.Status.PENDING:
        raise serializers.ValidationError(
            {
                "status": (
                    f"Only a 'pending' reservation can be rejected (this one is "
                    f"'{reservation.status}')."
                )
            }
        )
    reservation.status = Reservation.Status.REJECTED
    reservation.approver = actor
    reservation.approval_note = note
    reservation.save(update_fields=["status", "approver", "approval_note", "updated_at"])

    _emit_approval_decision_on_commit(reservation, approved=False, actor=actor, note=note)
    return reservation


def cancel_reservation(*, reservation: Reservation, actor) -> Reservation:
    """Cancel a still-active (`pending`/`approved`) reservation. `fulfilled`
    (already converted to a T3.3 checkout), `rejected`, `cancelled`, and
    `expired` reservations are not cancellable -- there is nothing left to
    free, or (fulfilled) the T3.3 checkout lifecycle owns ending it now."""
    if reservation.status not in (Reservation.Status.PENDING, Reservation.Status.APPROVED):
        raise serializers.ValidationError(
            {"status": (f"A '{reservation.status}' reservation cannot be cancelled.")}
        )
    reservation.status = Reservation.Status.CANCELLED
    reservation.save(update_fields=["status", "updated_at"])
    return reservation


# --- Domain events (transaction.on_commit, same pattern as
# `apps.stock.services._emit_low_stock_event_on_commit`) -----------------


def _emit_reservation_confirmed_on_commit(reservation: Reservation) -> None:
    def _send() -> None:
        reservation_confirmed.send_robust(
            sender=Reservation,
            reservation_id=reservation.id,
            tenant_id=reservation.tenant_id,
            asset_id=reservation.asset_id,
            user_id=reservation.user_id,
            start_at=reservation.start_at,
            end_at=reservation.end_at,
            occurred_at=timezone.now(),
        )

    transaction.on_commit(_send)


def _emit_approval_request_on_commit(reservation: Reservation) -> None:
    def _send() -> None:
        approval_request.send_robust(
            sender=Reservation,
            reservation_id=reservation.id,
            tenant_id=reservation.tenant_id,
            asset_id=reservation.asset_id,
            user_id=reservation.user_id,
            start_at=reservation.start_at,
            end_at=reservation.end_at,
            occurred_at=timezone.now(),
        )

    transaction.on_commit(_send)


def _emit_approval_decision_on_commit(
    reservation: Reservation, *, approved: bool, actor, note: str
) -> None:
    def _send() -> None:
        approval_decision.send_robust(
            sender=Reservation,
            reservation_id=reservation.id,
            tenant_id=reservation.tenant_id,
            asset_id=reservation.asset_id,
            user_id=reservation.user_id,
            start_at=reservation.start_at,
            end_at=reservation.end_at,
            approved=approved,
            approver_id=actor.id,
            note=note,
            occurred_at=timezone.now(),
        )

    transaction.on_commit(_send)
