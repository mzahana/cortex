"""Data-migration test (code-review finding): `0004_backfill_completed_
reservations` backfills `Reservation.status` `fulfilled` -> `completed` for
any reservation whose linked `Checkout` was ALREADY checked in before this
deploy -- otherwise such a reservation is stuck `fulfilled` forever (see the
migration's own module docstring for the full bug narrative).

No existing migration-execution test harness in this repo (checked
`apps/rbac`/`apps/projects`'s `RunPython` data migrations -- none have a
dedicated test module), so this imports the migration's own
`backfill_completed`/`noop_reverse` functions directly (via `importlib`,
since a migration module's filename isn't a valid Python identifier and
can't be `import`ed with dotted-attribute syntax) and drives them against
real rows created through the ORM inside `tenant_context`. This is the
smallest, most direct way to prove the exact backfill predicate without
standing up a full `MigrationExecutor` run -- the forward/backward round
trip against a real migration history is additionally verified manually,
per `add-migration`'s own checklist item 6, against a scratch DB, as part of
this change's overall verification.
"""

from __future__ import annotations

import importlib
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.common.tests.factories import AssetFactory, CategoryFactory, TenantFactory, UserFactory
from apps.reservations.models import Checkout, Reservation
from apps.tenancy.context import tenant_context

pytestmark = pytest.mark.django_db

_migration_module = importlib.import_module(
    "apps.reservations.migrations.0004_backfill_completed_reservations"
)
backfill_completed = _migration_module.backfill_completed
noop_reverse = _migration_module.noop_reverse


def _make_reservation_and_checkout(tenant, user, asset, *, reservation_status, checked_in):
    with tenant_context(tenant.id):
        reservation = Reservation.objects.create(
            tenant=tenant,
            asset=asset,
            user=user,
            start_at=timezone.now() - timedelta(days=2),
            end_at=timezone.now() - timedelta(days=1),
            status=reservation_status,
        )
        Checkout.objects.create(
            tenant=tenant,
            asset=asset,
            user=user,
            reservation=reservation,
            checked_out_at=timezone.now() - timedelta(days=2),
            due_at=timezone.now() - timedelta(days=1, hours=12),
            checked_in_at=(timezone.now() - timedelta(days=1) if checked_in else None),
        )
    return reservation


class TestBackfillCompletedReservations:
    def test_fulfilled_reservation_with_checked_in_checkout_is_completed(self):
        tenant = TenantFactory()
        user = UserFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        reservation = _make_reservation_and_checkout(
            tenant, user, asset, reservation_status=Reservation.Status.FULFILLED, checked_in=True
        )

        backfill_completed(apps=None, schema_editor=None)

        with tenant_context(tenant.id):
            reservation.refresh_from_db()
            assert reservation.status == Reservation.Status.COMPLETED

    def test_fulfilled_reservation_with_still_open_checkout_is_untouched(self):
        """The asset is still physically checked out (no checkin yet) --
        this reservation must stay `fulfilled` (still blocking, per
        `Reservation.ACTIVE_STATUSES`), not be prematurely completed."""
        tenant = TenantFactory()
        user = UserFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        reservation = _make_reservation_and_checkout(
            tenant, user, asset, reservation_status=Reservation.Status.FULFILLED, checked_in=False
        )

        backfill_completed(apps=None, schema_editor=None)

        with tenant_context(tenant.id):
            reservation.refresh_from_db()
            assert reservation.status == Reservation.Status.FULFILLED

    def test_non_fulfilled_reservations_are_never_touched(self):
        """Only `fulfilled` rows are eligible -- `approved`/`pending`/
        `cancelled`/`rejected`/`expired` reservations (even ones with some
        unrelated checked-in `Checkout` row) must be left exactly as-is."""
        tenant = TenantFactory()
        user = UserFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        with tenant_context(tenant.id):
            approved = Reservation.objects.create(
                tenant=tenant,
                asset=asset,
                user=user,
                start_at=timezone.now() + timedelta(days=1),
                end_at=timezone.now() + timedelta(days=2),
                status=Reservation.Status.APPROVED,
            )
            cancelled = Reservation.objects.create(
                tenant=tenant,
                asset=asset,
                user=user,
                start_at=timezone.now() - timedelta(days=5),
                end_at=timezone.now() - timedelta(days=4),
                status=Reservation.Status.CANCELLED,
            )

        backfill_completed(apps=None, schema_editor=None)

        with tenant_context(tenant.id):
            approved.refresh_from_db()
            cancelled.refresh_from_db()
            assert approved.status == Reservation.Status.APPROVED
            assert cancelled.status == Reservation.Status.CANCELLED

    def test_multiple_checkouts_linked_to_same_reservation_handled_defensively(self):
        """Task instruction: query for "any checkout with checked_in_at not
        null" rather than assuming `.get()`, in case more than one `Checkout`
        row is ever linked to the same `reservation_id`."""
        tenant = TenantFactory()
        user = UserFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        with tenant_context(tenant.id):
            reservation = Reservation.objects.create(
                tenant=tenant,
                asset=asset,
                user=user,
                start_at=timezone.now() - timedelta(days=2),
                end_at=timezone.now() - timedelta(days=1),
                status=Reservation.Status.FULFILLED,
            )
            # An open checkout AND a checked-in one, both pointing at the
            # same reservation -- the backfill must still fire because AT
            # LEAST one linked checkout is checked in.
            Checkout.objects.create(
                tenant=tenant,
                asset=asset,
                user=user,
                reservation=reservation,
                checked_out_at=timezone.now() - timedelta(days=2),
                due_at=timezone.now() - timedelta(days=1, hours=12),
                checked_in_at=None,
            )
            Checkout.objects.create(
                tenant=tenant,
                asset=asset,
                user=user,
                reservation=reservation,
                checked_out_at=timezone.now() - timedelta(days=2),
                due_at=timezone.now() - timedelta(days=1, hours=12),
                checked_in_at=timezone.now() - timedelta(days=1),
            )

        backfill_completed(apps=None, schema_editor=None)

        with tenant_context(tenant.id):
            reservation.refresh_from_db()
            assert reservation.status == Reservation.Status.COMPLETED

    def test_reverse_is_a_documented_noop(self):
        """The reverse migration is intentionally a no-op (see the migration
        module's docstring for why reversing is not a meaningful operation)
        -- confirm it doesn't raise and doesn't touch any rows."""
        tenant = TenantFactory()
        user = UserFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        reservation = _make_reservation_and_checkout(
            tenant, user, asset, reservation_status=Reservation.Status.FULFILLED, checked_in=True
        )
        backfill_completed(apps=None, schema_editor=None)

        noop_reverse(apps=None, schema_editor=None)

        with tenant_context(tenant.id):
            reservation.refresh_from_db()
            assert reservation.status == Reservation.Status.COMPLETED  # unchanged by the no-op
