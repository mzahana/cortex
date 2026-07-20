"""T3.6 — F4 acceptance: the no-overlap invariant proven at BOTH layers.

1. The app-layer service (`create_reservation`) rejects an overlapping window
   with a clean `409` (`ReservationConflict`), never a raw 500 — the common
   path a real client hits.
2. The DB-level GiST EXCLUDE constraint (`reservation_no_overlap_active`,
   migration `0002`) independently rejects a raw SQL INSERT that never goes
   through the ORM/service at all — the actual T3.1 exit criterion and the
   R4-style "missing app-level check is still caught by the DB" backstop
   proof CLAUDE.md requires for every DB-enforced invariant. Mirrors
   `apps.stock.tests.test_stock_db_immutability`'s pattern: real connection,
   real commit, the RLS-subject `cortex_app` role.

Also proves the two edges the exclusion constraint's WHERE clause and range
type are specifically designed around: an inactive (cancelled/rejected/
expired) reservation drops OUT of the constraint (rebooking the same window
must succeed), and back-to-back windows (`tstzrange`'s default `[)` bounds)
never conflict.
"""

from __future__ import annotations

from datetime import timedelta

import psycopg
import pytest
from django.utils import timezone

from apps.common.tests.factories import AssetFactory, TenantFactory, UserFactory
from apps.reservations.models import Reservation
from apps.reservations.services import ReservationConflict, create_reservation
from apps.tenancy.context import tenant_context
from conftest import set_app_role_tenant

pytestmark = pytest.mark.django_db


def _window(hours_from_now: int = 1, duration_hours: int = 2):
    start = timezone.now() + timedelta(hours=hours_from_now)
    end = start + timedelta(hours=duration_hours)
    return start, end


class TestAppLayerConflictRejection:
    def test_overlapping_active_reservation_rejected_with_clean_409(self):
        tenant = TenantFactory()
        asset = AssetFactory(tenant=tenant)
        user = UserFactory(tenant=tenant)
        other_user = UserFactory(tenant=tenant)
        start, end = _window()

        with tenant_context(tenant.id):
            create_reservation(tenant=tenant, actor=user, asset=asset, start_at=start, end_at=end)

            # Fully overlapping window, different requester.
            with pytest.raises(ReservationConflict) as exc_info:
                create_reservation(
                    tenant=tenant,
                    actor=other_user,
                    asset=asset,
                    start_at=start + timedelta(minutes=30),
                    end_at=end + timedelta(minutes=30),
                )
            assert exc_info.value.status_code == 409

            assert Reservation.objects.filter(asset=asset).count() == 1

    def test_cancelled_reservation_does_not_block_rebooking_same_window(self):
        tenant = TenantFactory()
        asset = AssetFactory(tenant=tenant)
        user = UserFactory(tenant=tenant)
        other_user = UserFactory(tenant=tenant)
        start, end = _window()

        with tenant_context(tenant.id):
            first = create_reservation(
                tenant=tenant, actor=user, asset=asset, start_at=start, end_at=end
            )
            first.status = Reservation.Status.CANCELLED
            first.save(update_fields=["status"])

            # Same exact window, now free because the only prior booking is
            # cancelled (dropped out of `Reservation.ACTIVE_STATUSES`).
            second = create_reservation(
                tenant=tenant, actor=other_user, asset=asset, start_at=start, end_at=end
            )
            assert second.id != first.id
            assert second.status in (
                Reservation.Status.APPROVED,
                Reservation.Status.PENDING,
            )

    def test_rejected_and_expired_reservations_also_do_not_block_rebooking(self):
        tenant = TenantFactory()
        asset = AssetFactory(tenant=tenant)
        user = UserFactory(tenant=tenant)
        start, end = _window()

        with tenant_context(tenant.id):
            for terminal_status in (Reservation.Status.REJECTED, Reservation.Status.EXPIRED):
                Reservation.objects.filter(asset=asset).delete()
                blocker = create_reservation(
                    tenant=tenant, actor=user, asset=asset, start_at=start, end_at=end
                )
                blocker.status = terminal_status
                blocker.save(update_fields=["status"])

                rebooked = create_reservation(
                    tenant=tenant, actor=user, asset=asset, start_at=start, end_at=end
                )
                assert rebooked.id != blocker.id

    def test_back_to_back_windows_do_not_conflict(self):
        """`[start_at, end_at)` semantics: one reservation ending exactly when
        the next begins must NOT be treated as an overlap."""
        tenant = TenantFactory()
        asset = AssetFactory(tenant=tenant)
        user = UserFactory(tenant=tenant)
        start, middle = _window()
        end = middle + timedelta(hours=1)

        with tenant_context(tenant.id):
            first = create_reservation(
                tenant=tenant, actor=user, asset=asset, start_at=start, end_at=middle
            )
            second = create_reservation(
                tenant=tenant, actor=user, asset=asset, start_at=middle, end_at=end
            )
            assert first.id != second.id
            assert Reservation.objects.filter(asset=asset).count() == 2


@pytest.mark.django_db(transaction=True)
def test_raw_sql_overlap_insert_is_rejected_by_the_gist_exclusion_constraint(app_role_connection):
    """The actual T3.1 exit criterion: a raw INSERT that never goes through
    Django/the service layer's pre-check is still rejected — the DB
    constraint, not the app code, is what ultimately guarantees F4.
    """
    tenant = TenantFactory()
    asset = AssetFactory(tenant=tenant)
    user = UserFactory(tenant=tenant)
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=2)

    with tenant_context(tenant.id):
        Reservation.objects.create(
            tenant=tenant,
            asset=asset,
            user=user,
            start_at=start,
            end_at=end,
            status=Reservation.Status.APPROVED,
        )

    set_app_role_tenant(app_role_connection, tenant.id)
    with app_role_connection.cursor() as cur:
        with pytest.raises(psycopg.errors.ExclusionViolation):
            cur.execute(
                """
                INSERT INTO reservations_reservation
                    (tenant_id, asset_id, user_id, start_at, end_at, status,
                     approval_note, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, 'pending', '', now(), now())
                """,
                [
                    tenant.id,
                    asset.id,
                    user.id,
                    start + timedelta(minutes=15),
                    end + timedelta(minutes=15),
                ],
            )
    app_role_connection.rollback()

    with app_role_connection.cursor() as cur:
        set_app_role_tenant(app_role_connection, tenant.id)
        cur.execute("SELECT count(*) FROM reservations_reservation WHERE asset_id = %s", [asset.id])
        assert cur.fetchone()[0] == 1  # the rejected INSERT never landed


@pytest.mark.django_db(transaction=True)
def test_raw_sql_non_overlapping_insert_for_same_asset_succeeds(app_role_connection):
    """Negative control for the test above: a genuinely non-overlapping
    window for the SAME asset is NOT rejected — proves the constraint is
    scoped to overlap, not merely "any second row for this asset"."""
    tenant = TenantFactory()
    asset = AssetFactory(tenant=tenant)
    user = UserFactory(tenant=tenant)
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=2)

    with tenant_context(tenant.id):
        Reservation.objects.create(
            tenant=tenant,
            asset=asset,
            user=user,
            start_at=start,
            end_at=end,
            status=Reservation.Status.APPROVED,
        )

    set_app_role_tenant(app_role_connection, tenant.id)
    later_start = end + timedelta(hours=1)
    later_end = later_start + timedelta(hours=1)
    with app_role_connection.cursor() as cur:
        cur.execute(
            """
            INSERT INTO reservations_reservation
                (tenant_id, asset_id, user_id, start_at, end_at, status,
                 approval_note, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, 'pending', '', now(), now())
            """,
            [tenant.id, asset.id, user.id, later_start, later_end],
        )
    app_role_connection.commit()

    with app_role_connection.cursor() as cur:
        set_app_role_tenant(app_role_connection, tenant.id)
        cur.execute("SELECT count(*) FROM reservations_reservation WHERE asset_id = %s", [asset.id])
        assert cur.fetchone()[0] == 2
