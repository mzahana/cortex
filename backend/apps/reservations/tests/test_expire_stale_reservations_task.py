"""Bug fix: `Reservation.Status.EXPIRED` existed on the model since T3.1 but
nothing ever set it. `apps.reservations.tasks.expire_stale_reservations` (the
Celery beat scan wired into `config/celery.py`'s `beat_schedule`) sweeps every
tenant's `pending`/`approved` reservations whose window has passed to
`expired`, freeing them from `Reservation.ACTIVE_STATUSES` and the `0002` GiST
exclusion constraint so the window becomes rebookable -- both at the app
pre-check level and at the DB constraint level.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.common.tests.factories import AssetFactory, CategoryFactory, TenantFactory, UserFactory
from apps.reservations.models import Reservation
from apps.reservations.tasks import expire_stale_reservations
from apps.tenancy.context import tenant_context

pytestmark = pytest.mark.django_db


class TestExpireStaleReservationsTask:
    def test_past_window_pending_reservation_is_expired_and_window_becomes_rebookable(self):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        start = timezone.now() - timedelta(hours=4)
        end = timezone.now() - timedelta(hours=2)
        with tenant_context(tenant.id):
            stale = Reservation.objects.create(
                tenant=tenant,
                asset=asset,
                user=member,
                start_at=start,
                end_at=end,
                status=Reservation.Status.PENDING,
            )

        expired_count = expire_stale_reservations()
        assert expired_count == 1

        with tenant_context(tenant.id):
            stale.refresh_from_db()
            assert stale.status == Reservation.Status.EXPIRED

            # App-level pre-check: the SAME window is no longer flagged as a
            # conflict (dropped out of `ACTIVE_STATUSES`).
            from apps.reservations.services import create_reservation

            new_reservation = create_reservation(
                tenant=tenant, actor=member, asset=asset, start_at=start, end_at=end
            )
            assert new_reservation.status in (
                Reservation.Status.PENDING,
                Reservation.Status.APPROVED,
            )

            # DB-level backstop: the exclusion constraint doesn't reject this
            # insert either (already proven by `create_reservation` above
            # succeeding without raising `IntegrityError`/`ReservationConflict`).

    def test_past_window_approved_reservation_is_expired(self):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        with tenant_context(tenant.id):
            stale = Reservation.objects.create(
                tenant=tenant,
                asset=asset,
                user=member,
                start_at=timezone.now() - timedelta(hours=4),
                end_at=timezone.now() - timedelta(hours=2),
                status=Reservation.Status.APPROVED,
            )

        expired_count = expire_stale_reservations()
        assert expired_count == 1

        with tenant_context(tenant.id):
            stale.refresh_from_db()
            assert stale.status == Reservation.Status.EXPIRED

    def test_reservation_not_yet_past_its_window_is_untouched(self):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        with tenant_context(tenant.id):
            future = Reservation.objects.create(
                tenant=tenant,
                asset=asset,
                user=member,
                start_at=timezone.now() + timedelta(hours=1),
                end_at=timezone.now() + timedelta(hours=3),
                status=Reservation.Status.PENDING,
            )

        expired_count = expire_stale_reservations()
        assert expired_count == 0

        with tenant_context(tenant.id):
            future.refresh_from_db()
            assert future.status == Reservation.Status.PENDING  # untouched

    @pytest.mark.parametrize(
        "status",
        [
            Reservation.Status.FULFILLED,
            Reservation.Status.COMPLETED,
            Reservation.Status.CANCELLED,
            Reservation.Status.REJECTED,
            Reservation.Status.EXPIRED,
        ],
    )
    def test_terminal_and_fulfilled_reservations_past_window_are_untouched(self, status):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        with tenant_context(tenant.id):
            reservation = Reservation.objects.create(
                tenant=tenant,
                asset=asset,
                user=member,
                start_at=timezone.now() - timedelta(hours=4),
                end_at=timezone.now() - timedelta(hours=2),
                status=status,
            )

        expired_count = expire_stale_reservations()
        assert expired_count == 0

        with tenant_context(tenant.id):
            reservation.refresh_from_db()
            assert reservation.status == status  # untouched

    def test_sweep_respects_tenant_isolation(self):
        tenant_a = TenantFactory()
        member_a = UserFactory(tenant=tenant_a)
        asset_a = AssetFactory(tenant=tenant_a, category=CategoryFactory(tenant=tenant_a))
        tenant_b = TenantFactory()
        member_b = UserFactory(tenant=tenant_b)
        asset_b = AssetFactory(tenant=tenant_b, category=CategoryFactory(tenant=tenant_b))

        with tenant_context(tenant_a.id):
            stale_a = Reservation.objects.create(
                tenant=tenant_a,
                asset=asset_a,
                user=member_a,
                start_at=timezone.now() - timedelta(hours=4),
                end_at=timezone.now() - timedelta(hours=2),
                status=Reservation.Status.PENDING,
            )
        with tenant_context(tenant_b.id):
            stale_b = Reservation.objects.create(
                tenant=tenant_b,
                asset=asset_b,
                user=member_b,
                start_at=timezone.now() - timedelta(hours=4),
                end_at=timezone.now() - timedelta(hours=2),
                status=Reservation.Status.PENDING,
            )

        expired_count = expire_stale_reservations()
        assert expired_count == 2

        with tenant_context(tenant_a.id):
            stale_a.refresh_from_db()
            assert stale_a.status == Reservation.Status.EXPIRED
        with tenant_context(tenant_b.id):
            stale_b.refresh_from_db()
            assert stale_b.status == Reservation.Status.EXPIRED
