"""T3.6 — F5 acceptance: checkout sets `Asset.status = in_use` + creates an
open `Checkout`; checkin records condition and frees the asset (idempotent);
overdue detection (`?overdue=true` and `is_overdue`); `checkout.override`
scoped RBAC + audit; a reservation cannot be hijacked by another user; and
cross-tenant isolation (R4).
"""

from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.assets.models import Asset
from apps.audit.models import AuditLog
from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
    AssetFactory,
    CategoryFactory,
    ProjectFactory,
    TenantFactory,
    UserFactory,
    add_project_membership,
    upgrade_tenant_wide_role,
)
from apps.rbac.permission_keys import ROLE_ADMIN, ROLE_PROJECT_LEAD
from apps.reservations.checkout import CheckoutSerializer
from apps.reservations.models import Checkout, Reservation
from apps.tenancy.context import tenant_context

pytestmark = pytest.mark.django_db


def _login(client, tenant, user):
    response = client.post(
        "/api/v1/auth/login",
        {"tenant": tenant.slug, "email": user.email, "password": DEFAULT_TEST_PASSWORD},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return response


def _iso(dt) -> str:
    return dt.isoformat()


def _checkout_payload(asset, due_at=None):
    due_at = due_at or (timezone.now() + timedelta(days=1))
    return {"asset": asset.id, "due_at": _iso(due_at)}


class TestCheckoutLifecycle:
    def test_checkout_sets_asset_in_use_and_creates_open_checkout(self, client):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))

        _login(client, tenant, member)
        response = client.post(
            "/api/v1/checkouts/",
            data=json.dumps(_checkout_payload(asset)),
            content_type="application/json",
        )
        assert response.status_code == 201, response.content
        body = response.json()
        assert body["checked_in_at"] is None
        assert body["is_open"] is True

        with tenant_context(tenant.id):
            asset.refresh_from_db()
            assert asset.status == Asset.Status.IN_USE
            checkout = Checkout.objects.get(pk=body["id"])
            assert checkout.checked_in_at is None

        entries = AuditLog.all_objects.filter(
            tenant_id=tenant.id, entity_type="checkout", entity_id=body["id"]
        )
        assert entries.count() == 1
        assert entries.first().action == "checkout.manage"

    def test_checkin_records_condition_and_frees_the_asset(self, client):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))

        _login(client, tenant, member)
        create_response = client.post(
            "/api/v1/checkouts/",
            data=json.dumps(_checkout_payload(asset)),
            content_type="application/json",
        )
        checkout_id = create_response.json()["id"]

        checkin_response = client.post(
            f"/api/v1/checkouts/{checkout_id}/checkin/",
            data=json.dumps({"checkin_condition": "minor scuff, still functional"}),
            content_type="application/json",
        )
        assert checkin_response.status_code == 200, checkin_response.content
        body = checkin_response.json()
        assert body["checked_in_at"] is not None
        assert body["checkin_condition"] == "minor scuff, still functional"
        assert body["is_open"] is False

        with tenant_context(tenant.id):
            asset.refresh_from_db()
            assert asset.status == Asset.Status.AVAILABLE

    def test_checkin_is_idempotent_no_error_no_double_audit_no_corruption(self, client):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))

        _login(client, tenant, member)
        create_response = client.post(
            "/api/v1/checkouts/",
            data=json.dumps(_checkout_payload(asset)),
            content_type="application/json",
        )
        checkout_id = create_response.json()["id"]

        first = client.post(
            f"/api/v1/checkouts/{checkout_id}/checkin/",
            data=json.dumps({"checkin_condition": "fine"}),
            content_type="application/json",
        )
        assert first.status_code == 200, first.content
        first_checked_in_at = first.json()["checked_in_at"]

        second = client.post(
            f"/api/v1/checkouts/{checkout_id}/checkin/",
            data=json.dumps({"checkin_condition": "trying to overwrite"}),
            content_type="application/json",
        )
        assert second.status_code == 200, second.content
        # No-op: the ORIGINAL checkin timestamp/condition is preserved, not
        # overwritten by the second call.
        assert second.json()["checked_in_at"] == first_checked_in_at
        assert second.json()["checkin_condition"] == "fine"

        entries = AuditLog.all_objects.filter(
            tenant_id=tenant.id, entity_type="checkout", entity_id=checkout_id
        )
        # 1 for create + 1 for the FIRST (effective) checkin; the second,
        # no-op checkin must not add a duplicate entry.
        assert entries.count() == 2

        with tenant_context(tenant.id):
            asset.refresh_from_db()
            assert asset.status == Asset.Status.AVAILABLE  # unchanged/consistent

    def test_overdue_checkout_is_flagged_by_filter_and_property(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        overdue_asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        current_asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))

        with tenant_context(tenant.id):
            overdue_checkout = Checkout.objects.create(
                tenant=tenant,
                asset=overdue_asset,
                user=admin,
                checked_out_at=timezone.now() - timedelta(days=5),
                due_at=timezone.now() - timedelta(days=1),  # in the past
            )
            assert overdue_checkout.is_overdue is True

        _login(client, tenant, admin)
        current_response = client.post(
            "/api/v1/checkouts/",
            data=json.dumps(_checkout_payload(current_asset)),
            content_type="application/json",
        )
        assert current_response.status_code == 201, current_response.content

        response = client.get("/api/v1/checkouts/?overdue=true")
        assert response.status_code == 200, response.content
        ids = {row["id"] for row in response.json()["results"]}
        assert ids == {overdue_checkout.id}

    def test_open_filter_excludes_checked_in_items(self, client):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))

        _login(client, tenant, member)
        create_response = client.post(
            "/api/v1/checkouts/",
            data=json.dumps(_checkout_payload(asset)),
            content_type="application/json",
        )
        checkout_id = create_response.json()["id"]
        client.post(f"/api/v1/checkouts/{checkout_id}/checkin/", content_type="application/json")

        open_response = client.get("/api/v1/checkouts/?open=true")
        assert open_response.status_code == 200, open_response.content
        assert checkout_id not in {row["id"] for row in open_response.json()["results"]}

        closed_response = client.get("/api/v1/checkouts/?open=false")
        assert checkout_id in {row["id"] for row in closed_response.json()["results"]}


class TestOverrideReturn:
    def test_override_return_requires_checkout_override_scope(self, client):
        tenant = TenantFactory()
        holder = UserFactory(tenant=tenant)
        plain_member = UserFactory(tenant=tenant)  # no checkout.override anywhere
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))

        _login(client, tenant, holder)
        create_response = client.post(
            "/api/v1/checkouts/",
            data=json.dumps(_checkout_payload(asset)),
            content_type="application/json",
        )
        checkout_id = create_response.json()["id"]
        client.post("/api/v1/auth/logout")

        _login(client, tenant, plain_member)
        denied = client.post(
            f"/api/v1/checkouts/{checkout_id}/override-return/",
            data=json.dumps({"checkin_condition": "forced"}),
            content_type="application/json",
        )
        assert denied.status_code == 403, denied.content

        with tenant_context(tenant.id):
            checkout = Checkout.objects.get(pk=checkout_id)
            assert checkout.checked_in_at is None  # untouched by the denied attempt

    def test_admin_override_return_is_always_audited(self, client):
        tenant = TenantFactory()
        holder = UserFactory(tenant=tenant)
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))

        _login(client, tenant, holder)
        create_response = client.post(
            "/api/v1/checkouts/",
            data=json.dumps(_checkout_payload(asset)),
            content_type="application/json",
        )
        checkout_id = create_response.json()["id"]
        client.post("/api/v1/auth/logout")

        _login(client, tenant, admin)
        response = client.post(
            f"/api/v1/checkouts/{checkout_id}/override-return/",
            data=json.dumps({"checkin_condition": "force-returned by admin"}),
            content_type="application/json",
        )
        assert response.status_code == 200, response.content
        assert response.json()["checked_in_at"] is not None

        with tenant_context(tenant.id):
            asset.refresh_from_db()
            assert asset.status == Asset.Status.AVAILABLE

        entries = AuditLog.all_objects.filter(
            tenant_id=tenant.id, entity_type="checkout", entity_id=checkout_id
        )
        assert entries.filter(action="checkout.override").count() == 1

    def test_project_lead_override_scoped_to_own_project(self, client):
        tenant = TenantFactory()
        own_project = ProjectFactory(tenant=tenant)
        other_project = ProjectFactory(tenant=tenant)
        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, own_project, ROLE_PROJECT_LEAD)
        holder = UserFactory(tenant=tenant)

        own_asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        other_asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        with tenant_context(tenant.id):
            own_asset.project = own_project
            own_asset.save(update_fields=["project"])
            other_asset.project = other_project
            other_asset.save(update_fields=["project"])

        _login(client, tenant, holder)
        own_checkout = client.post(
            "/api/v1/checkouts/",
            data=json.dumps(_checkout_payload(own_asset)),
            content_type="application/json",
        ).json()["id"]
        other_checkout = client.post(
            "/api/v1/checkouts/",
            data=json.dumps(_checkout_payload(other_asset)),
            content_type="application/json",
        ).json()["id"]
        client.post("/api/v1/auth/logout")

        _login(client, tenant, lead)
        allowed = client.post(
            f"/api/v1/checkouts/{own_checkout}/override-return/",
            content_type="application/json",
        )
        assert allowed.status_code == 200, allowed.content

        denied = client.post(
            f"/api/v1/checkouts/{other_checkout}/override-return/",
            content_type="application/json",
        )
        assert denied.status_code == 403, denied.content


class TestReservationLinkedCheckout:
    def test_checkout_can_use_own_approved_reservation(self, client):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        with tenant_context(tenant.id):
            reservation = Reservation.objects.create(
                tenant=tenant,
                asset=asset,
                user=member,
                start_at=timezone.now() - timedelta(minutes=5),
                end_at=timezone.now() + timedelta(hours=2),
                status=Reservation.Status.APPROVED,
            )

        _login(client, tenant, member)
        payload = _checkout_payload(asset)
        payload["reservation"] = reservation.id
        response = client.post(
            "/api/v1/checkouts/", data=json.dumps(payload), content_type="application/json"
        )
        assert response.status_code == 201, response.content
        assert response.json()["reservation"] == reservation.id

        # Code-review finding: the linked reservation must flip to
        # `fulfilled` in the same atomic block as the checkout create, so
        # `cancel_reservation` can never later free this window while the
        # asset is still physically checked out (F4/T3.3 seam).
        with tenant_context(tenant.id):
            reservation.refresh_from_db()
            assert reservation.status == Reservation.Status.FULFILLED

    def test_fulfilled_reservation_cannot_be_cancelled(self, client):
        """Once a checkout has been created against a reservation (-> now
        `fulfilled`), `POST /reservations/{id}/cancel` must reject it (400)
        rather than silently accepting and freeing the exclusion-constraint
        window for a still-checked-out asset."""
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        with tenant_context(tenant.id):
            reservation = Reservation.objects.create(
                tenant=tenant,
                asset=asset,
                user=member,
                start_at=timezone.now() - timedelta(minutes=5),
                end_at=timezone.now() + timedelta(hours=2),
                status=Reservation.Status.APPROVED,
            )

        _login(client, tenant, member)
        payload = _checkout_payload(asset)
        payload["reservation"] = reservation.id
        checkout_response = client.post(
            "/api/v1/checkouts/", data=json.dumps(payload), content_type="application/json"
        )
        assert checkout_response.status_code == 201, checkout_response.content

        cancel_response = client.post(f"/api/v1/reservations/{reservation.id}/cancel/")
        assert cancel_response.status_code == 400, cancel_response.content

        with tenant_context(tenant.id):
            reservation.refresh_from_db()
            assert reservation.status == Reservation.Status.FULFILLED  # unchanged

    def test_fulfilled_reservation_window_still_blocks_a_new_overlapping_reservation(self, client):
        """`FULFILLED` deliberately stays in `Reservation.ACTIVE_STATUSES`
        (unlike `cancelled`/`rejected`/`expired`): the asset is physically
        checked out for this window, so a second overlapping reservation
        must still be rejected exactly as it would be against an `approved`
        one — the difference from those terminal statuses is that a
        fulfilled reservation is only ever ended by the T3.3 checkout
        lifecycle (checkin/override-return), never by `cancel`."""
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        other_member = UserFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        start = timezone.now() - timedelta(minutes=5)
        end = timezone.now() + timedelta(hours=2)
        with tenant_context(tenant.id):
            reservation = Reservation.objects.create(
                tenant=tenant,
                asset=asset,
                user=member,
                start_at=start,
                end_at=end,
                status=Reservation.Status.APPROVED,
            )

        _login(client, tenant, member)
        payload = _checkout_payload(asset)
        payload["reservation"] = reservation.id
        checkout_response = client.post(
            "/api/v1/checkouts/", data=json.dumps(payload), content_type="application/json"
        )
        assert checkout_response.status_code == 201, checkout_response.content

        _login(client, tenant, other_member)
        overlap_response = client.post(
            "/api/v1/reservations/",
            data=json.dumps(
                {
                    "asset": asset.id,
                    "start_at": _iso(start + timedelta(minutes=30)),
                    "end_at": _iso(end + timedelta(minutes=30)),
                }
            ),
            content_type="application/json",
        )
        assert overlap_response.status_code == 409, overlap_response.content

    def test_reservation_cannot_be_hijacked_by_another_user(self, client):
        """R4/F5-adjacent guard: `attrs["reservation"]` must belong to the
        REQUESTING user — another user cannot check out an asset by citing
        someone else's approved reservation id."""
        tenant = TenantFactory()
        owner = UserFactory(tenant=tenant)
        hijacker = UserFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        with tenant_context(tenant.id):
            reservation = Reservation.objects.create(
                tenant=tenant,
                asset=asset,
                user=owner,
                start_at=timezone.now() - timedelta(minutes=5),
                end_at=timezone.now() + timedelta(hours=2),
                status=Reservation.Status.APPROVED,
            )

        _login(client, tenant, hijacker)
        payload = _checkout_payload(asset)
        payload["reservation"] = reservation.id
        response = client.post(
            "/api/v1/checkouts/", data=json.dumps(payload), content_type="application/json"
        )
        assert response.status_code == 400, response.content

        with tenant_context(tenant.id):
            asset.refresh_from_db()
            assert asset.status == Asset.Status.AVAILABLE  # never checked out


class TestFulfilledReservationCompletesOnCheckin:
    """Bug fix (F4 gap): a `fulfilled` reservation must free its window once
    the asset is actually checked back in, not stay `fulfilled` forever.
    `checkin`/`override-return` now move it to the new terminal `completed`
    status (dropped from `Reservation.ACTIVE_STATUSES`), while
    `test_fulfilled_reservation_window_still_blocks_a_new_overlapping_reservation`
    (above) proves the window still blocks BEFORE checkin -- these two tests
    together pin the full before/after behavior."""

    def test_checkin_completes_the_reservation_and_frees_the_window(self, client):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        other_member = UserFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        start = timezone.now() - timedelta(minutes=5)
        end = timezone.now() + timedelta(hours=2)
        with tenant_context(tenant.id):
            reservation = Reservation.objects.create(
                tenant=tenant,
                asset=asset,
                user=member,
                start_at=start,
                end_at=end,
                status=Reservation.Status.APPROVED,
            )

        _login(client, tenant, member)
        payload = _checkout_payload(asset)
        payload["reservation"] = reservation.id
        checkout_response = client.post(
            "/api/v1/checkouts/", data=json.dumps(payload), content_type="application/json"
        )
        assert checkout_response.status_code == 201, checkout_response.content
        checkout_id = checkout_response.json()["id"]

        checkin_response = client.post(
            f"/api/v1/checkouts/{checkout_id}/checkin/",
            data=json.dumps({"checkin_condition": "returned in good shape"}),
            content_type="application/json",
        )
        assert checkin_response.status_code == 200, checkin_response.content

        with tenant_context(tenant.id):
            reservation.refresh_from_db()
            assert reservation.status == Reservation.Status.COMPLETED
            asset.refresh_from_db()
            assert asset.status == Asset.Status.AVAILABLE

            log = AuditLog.objects.filter(entity_type="checkout", entity_id=checkout_id).latest(
                "created_at"
            )
            assert log.after["reservation"] == {
                "id": reservation.id,
                "status": Reservation.Status.COMPLETED,
            }
            assert log.before["reservation"] == {
                "id": reservation.id,
                "status": Reservation.Status.FULFILLED,
            }

        # The window is genuinely free now: another user can book the same
        # (or an overlapping) window without a 409, unlike the still-fulfilled
        # case in `test_fulfilled_reservation_window_still_blocks_a_new_overlapping_reservation`.
        _login(client, tenant, other_member)
        rebooking_response = client.post(
            "/api/v1/reservations/",
            data=json.dumps(
                {
                    "asset": asset.id,
                    "start_at": _iso(start + timedelta(minutes=30)),
                    "end_at": _iso(end + timedelta(minutes=30)),
                }
            ),
            content_type="application/json",
        )
        assert rebooking_response.status_code == 201, rebooking_response.content

    def test_override_return_also_completes_the_reservation(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        member = UserFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        with tenant_context(tenant.id):
            reservation = Reservation.objects.create(
                tenant=tenant,
                asset=asset,
                user=member,
                start_at=timezone.now() - timedelta(minutes=5),
                end_at=timezone.now() + timedelta(hours=2),
                status=Reservation.Status.APPROVED,
            )

        _login(client, tenant, member)
        payload = _checkout_payload(asset)
        payload["reservation"] = reservation.id
        checkout_response = client.post(
            "/api/v1/checkouts/", data=json.dumps(payload), content_type="application/json"
        )
        assert checkout_response.status_code == 201, checkout_response.content
        checkout_id = checkout_response.json()["id"]

        _login(client, tenant, admin)
        override_response = client.post(
            f"/api/v1/checkouts/{checkout_id}/override-return/",
            data=json.dumps({"checkin_condition": "force-returned"}),
            content_type="application/json",
        )
        assert override_response.status_code == 200, override_response.content

        with tenant_context(tenant.id):
            reservation.refresh_from_db()
            assert reservation.status == Reservation.Status.COMPLETED

    def test_walk_up_checkout_with_no_reservation_checkin_is_unaffected(self, client):
        """No reservation linked -- `perform_checkin` must not touch anything
        reservation-related and the audit `before`/`after` must not gain a
        `reservation` key."""
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))

        _login(client, tenant, member)
        checkout_response = client.post(
            "/api/v1/checkouts/",
            data=json.dumps(_checkout_payload(asset)),
            content_type="application/json",
        )
        checkout_id = checkout_response.json()["id"]

        checkin_response = client.post(
            f"/api/v1/checkouts/{checkout_id}/checkin/",
            data=json.dumps({"checkin_condition": "fine"}),
            content_type="application/json",
        )
        assert checkin_response.status_code == 200, checkin_response.content

        with tenant_context(tenant.id):
            log = AuditLog.objects.filter(entity_type="checkout", entity_id=checkout_id).latest(
                "created_at"
            )
            assert "reservation" not in log.before
            assert "reservation" not in log.after


class TestCheckoutWindowEnforcement:
    """Checkout of a reservation-backed asset is only allowed within the
    approved window `[start_at, end_at)` — checking out ahead of a
    future-dated APPROVED reservation, or after its window has already
    closed, must be rejected rather than silently allowed. A walk-up
    checkout (no `reservation`) has no window to check and is unaffected."""

    def test_checkout_before_start_at_is_rejected(self, client):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        with tenant_context(tenant.id):
            reservation = Reservation.objects.create(
                tenant=tenant,
                asset=asset,
                user=member,
                start_at=timezone.now() + timedelta(hours=2),
                end_at=timezone.now() + timedelta(hours=4),
                status=Reservation.Status.APPROVED,
            )

        _login(client, tenant, member)
        payload = _checkout_payload(asset)
        payload["reservation"] = reservation.id
        response = client.post(
            "/api/v1/checkouts/", data=json.dumps(payload), content_type="application/json"
        )
        assert response.status_code == 400, response.content
        assert "reservation" in response.json()["errors"]

        with tenant_context(tenant.id):
            asset.refresh_from_db()
            assert asset.status == Asset.Status.AVAILABLE  # never checked out
            reservation.refresh_from_db()
            assert reservation.status == Reservation.Status.APPROVED  # unchanged

    def test_checkout_exactly_at_start_at_succeeds(self, client):
        """Pin `timezone.now()` to EXACTLY `reservation.start_at` for the
        whole request (login + POST) so the real wall clock advancing past
        `start_at` before `validate()` runs can't hide a true off-by-one at
        the boundary -- a real login+POST without patching `now()` doesn't
        actually test "exactly at start_at" (code-review finding)."""
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        start = timezone.now()
        with tenant_context(tenant.id):
            reservation = Reservation.objects.create(
                tenant=tenant,
                asset=asset,
                user=member,
                start_at=start,
                end_at=start + timedelta(hours=2),
                status=Reservation.Status.APPROVED,
            )

        with patch("django.utils.timezone.now", return_value=start):
            _login(client, tenant, member)
            payload = _checkout_payload(asset, due_at=start + timedelta(days=1))
            payload["reservation"] = reservation.id
            response = client.post(
                "/api/v1/checkouts/", data=json.dumps(payload), content_type="application/json"
            )
        assert response.status_code == 201, response.content

        with tenant_context(tenant.id):
            reservation.refresh_from_db()
            assert reservation.status == Reservation.Status.FULFILLED

    def test_checkout_exactly_at_end_at_is_rejected(self, client):
        """The window is half-open `[start_at, end_at)` — exactly `end_at`
        must be rejected, not accepted. Previously untested at all."""
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        start = timezone.now() - timedelta(hours=2)
        end = timezone.now()
        with tenant_context(tenant.id):
            reservation = Reservation.objects.create(
                tenant=tenant,
                asset=asset,
                user=member,
                start_at=start,
                end_at=end,
                status=Reservation.Status.APPROVED,
            )

        with patch("django.utils.timezone.now", return_value=end):
            _login(client, tenant, member)
            payload = _checkout_payload(asset, due_at=end + timedelta(days=1))
            payload["reservation"] = reservation.id
            response = client.post(
                "/api/v1/checkouts/", data=json.dumps(payload), content_type="application/json"
            )
        assert response.status_code == 400, response.content
        assert "reservation" in response.json()["errors"]

        with tenant_context(tenant.id):
            reservation.refresh_from_db()
            assert reservation.status == Reservation.Status.APPROVED  # unchanged

    def test_checkout_after_end_at_is_rejected(self, client):
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
                status=Reservation.Status.APPROVED,
            )

        _login(client, tenant, member)
        payload = _checkout_payload(asset)
        payload["reservation"] = reservation.id
        response = client.post(
            "/api/v1/checkouts/", data=json.dumps(payload), content_type="application/json"
        )
        assert response.status_code == 400, response.content
        assert "reservation" in response.json()["errors"]

        with tenant_context(tenant.id):
            asset.refresh_from_db()
            assert asset.status == Asset.Status.AVAILABLE  # never checked out
            reservation.refresh_from_db()
            assert reservation.status == Reservation.Status.APPROVED  # unchanged

    def test_walk_up_checkout_has_no_window_to_check(self, client):
        """No `reservation` supplied -- the window check must not apply."""
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))

        _login(client, tenant, member)
        response = client.post(
            "/api/v1/checkouts/",
            data=json.dumps(_checkout_payload(asset)),
            content_type="application/json",
        )
        assert response.status_code == 201, response.content


class TestAssetAndReservationFilters:
    """`?asset=<id>`/`?reservation=<id>` manual query-param filters on
    `GET /checkouts` (added alongside `?open=`/`?overdue=`): non-numeric
    values resolve to an empty list (never a 500/400), and both stay
    tenant-scoped even against a cross-tenant id."""

    def test_asset_filter_returns_only_that_assets_checkouts(self, client):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        asset_a = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        asset_b = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))

        _login(client, tenant, member)
        checkout_a = client.post(
            "/api/v1/checkouts/",
            data=json.dumps(_checkout_payload(asset_a)),
            content_type="application/json",
        ).json()["id"]
        client.post(
            "/api/v1/checkouts/",
            data=json.dumps(_checkout_payload(asset_b)),
            content_type="application/json",
        )

        response = client.get(f"/api/v1/checkouts/?asset={asset_a.id}")
        assert response.status_code == 200, response.content
        ids = {row["id"] for row in response.json()["results"]}
        assert ids == {checkout_a}

    def test_reservation_filter_returns_only_the_linked_checkout(self, client):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        other_asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        with tenant_context(tenant.id):
            reservation = Reservation.objects.create(
                tenant=tenant,
                asset=asset,
                user=member,
                start_at=timezone.now() - timedelta(minutes=5),
                end_at=timezone.now() + timedelta(hours=2),
                status=Reservation.Status.APPROVED,
            )

        _login(client, tenant, member)
        payload = _checkout_payload(asset)
        payload["reservation"] = reservation.id
        linked_checkout = client.post(
            "/api/v1/checkouts/", data=json.dumps(payload), content_type="application/json"
        ).json()["id"]
        client.post(
            "/api/v1/checkouts/",
            data=json.dumps(_checkout_payload(other_asset)),
            content_type="application/json",
        )

        response = client.get(f"/api/v1/checkouts/?reservation={reservation.id}")
        assert response.status_code == 200, response.content
        ids = {row["id"] for row in response.json()["results"]}
        assert ids == {linked_checkout}

    def test_non_numeric_asset_and_reservation_params_yield_empty_not_error(self, client):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))

        _login(client, tenant, member)
        client.post(
            "/api/v1/checkouts/",
            data=json.dumps(_checkout_payload(asset)),
            content_type="application/json",
        )

        for param, value in (("asset", "not-a-number"), ("reservation", "abc"), ("asset", "")):
            response = client.get(f"/api/v1/checkouts/?{param}={value}")
            assert response.status_code == 200, (param, value, response.content)
            assert response.json()["results"] == []

    def test_asset_filter_combines_with_open_filter(self, client):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))

        _login(client, tenant, member)
        checkout_id = client.post(
            "/api/v1/checkouts/",
            data=json.dumps(_checkout_payload(asset)),
            content_type="application/json",
        ).json()["id"]
        client.post(f"/api/v1/checkouts/{checkout_id}/checkin/", content_type="application/json")

        open_response = client.get(f"/api/v1/checkouts/?asset={asset.id}&open=true")
        assert open_response.status_code == 200, open_response.content
        assert open_response.json()["results"] == []

        closed_response = client.get(f"/api/v1/checkouts/?asset={asset.id}&open=false")
        assert closed_response.status_code == 200, closed_response.content
        assert {row["id"] for row in closed_response.json()["results"]} == {checkout_id}

    def test_cross_tenant_asset_id_yields_empty_not_another_tenants_checkouts(self, client):
        """R4: a caller in tenant A filtering `?asset=<id belonging to
        tenant B>` must get an empty list, never tenant B's checkout(s) —
        the base queryset is already tenant-scoped BEFORE this filter is
        applied, so this proves the added filter doesn't reintroduce a leak
        via a raw `asset_id=int(...)` lookup that skips tenant scoping."""
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)

        other_tenant = TenantFactory()
        other_category = CategoryFactory(tenant=other_tenant)
        other_asset = AssetFactory(tenant=other_tenant, category=other_category)
        other_user = UserFactory(tenant=other_tenant)
        with tenant_context(other_tenant.id):
            Checkout.objects.create(
                tenant=other_tenant,
                asset=other_asset,
                user=other_user,
                checked_out_at=timezone.now(),
                due_at=timezone.now() + timedelta(days=1),
            )

        _login(client, tenant, admin)
        response = client.get(f"/api/v1/checkouts/?asset={other_asset.id}")
        assert response.status_code == 200, response.content
        assert response.json()["results"] == []

    def test_cross_tenant_reservation_id_yields_empty_not_another_tenants_checkouts(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)

        other_tenant = TenantFactory()
        other_category = CategoryFactory(tenant=other_tenant)
        other_asset = AssetFactory(tenant=other_tenant, category=other_category)
        other_user = UserFactory(tenant=other_tenant)
        with tenant_context(other_tenant.id):
            other_reservation = Reservation.objects.create(
                tenant=other_tenant,
                asset=other_asset,
                user=other_user,
                start_at=timezone.now() - timedelta(minutes=5),
                end_at=timezone.now() + timedelta(hours=2),
                status=Reservation.Status.APPROVED,
            )
            Checkout.objects.create(
                tenant=other_tenant,
                asset=other_asset,
                user=other_user,
                reservation=other_reservation,
                checked_out_at=timezone.now(),
                due_at=timezone.now() + timedelta(days=1),
            )

        _login(client, tenant, admin)
        response = client.get(f"/api/v1/checkouts/?reservation={other_reservation.id}")
        assert response.status_code == 200, response.content
        assert response.json()["results"] == []


class TestCrossTenantIsolation:
    def test_checkout_in_another_tenant_404s(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)

        other_tenant = TenantFactory()
        other_category = CategoryFactory(tenant=other_tenant)
        other_asset = AssetFactory(tenant=other_tenant, category=other_category)
        other_user = UserFactory(tenant=other_tenant)
        with tenant_context(other_tenant.id):
            other_checkout = Checkout.objects.create(
                tenant=other_tenant,
                asset=other_asset,
                user=other_user,
                checked_out_at=timezone.now(),
                due_at=timezone.now() + timedelta(days=1),
            )

        _login(client, tenant, admin)
        response = client.get(f"/api/v1/checkouts/{other_checkout.id}/")
        assert response.status_code == 404, response.content

        checkin_response = client.post(f"/api/v1/checkouts/{other_checkout.id}/checkin/")
        assert checkin_response.status_code == 404, checkin_response.content

        override_response = client.post(f"/api/v1/checkouts/{other_checkout.id}/override-return/")
        assert override_response.status_code in (403, 404), override_response.content


class TestCheckoutCreateToctouReservationStatusRecheck:
    """Code-review finding: `CheckoutSerializer.create`'s re-assertion that
    `locked_reservation.status in RESERVATION_CHECKOUT_STATUSES` (under the
    row lock) had zero test coverage — only `validate()`'s unlocked read was
    exercised. Directly drives the serializer so the reservation's status can
    be flipped in the DB AFTER `validate()` passes but BEFORE `create()`
    re-reads it under `select_for_update()`, simulating the genuine
    concurrent-request race this guard exists for."""

    def test_create_rejects_when_reservation_is_cancelled_between_validate_and_lock(self, client):
        """`COMPLETED` was moved INTO `RESERVATION_CHECKOUT_STATUSES` (bug
        fix, product decision: a `completed` reservation may re-back a new
        checkout within its original window), so it no longer models an
        ineligible status for this TOCTOU race -- `CANCELLED` (a status that
        stays ineligible in every case) is used here instead to keep
        exercising the same re-check under the lock."""
        from rest_framework.exceptions import ValidationError
        from rest_framework.test import APIRequestFactory

        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        with tenant_context(tenant.id):
            reservation = Reservation.objects.create(
                tenant=tenant,
                asset=asset,
                user=member,
                start_at=timezone.now() - timedelta(minutes=5),
                end_at=timezone.now() + timedelta(hours=2),
                status=Reservation.Status.APPROVED,
            )

        request = APIRequestFactory().post("/api/v1/checkouts/")
        request.user = member

        with tenant_context(tenant.id):
            serializer = CheckoutSerializer(
                data={
                    "asset": asset.id,
                    "due_at": _iso(timezone.now() + timedelta(days=1)),
                    "reservation": reservation.id,
                },
                context={"request": request},
            )
            assert serializer.is_valid(), serializer.errors

            # Simulate a concurrent request cancelling this SAME
            # reservation-backed checkout in the gap between this
            # `validate()` having already passed and `create()`'s own
            # `select_for_update()` lock below.
            reservation.status = Reservation.Status.CANCELLED
            reservation.save(update_fields=["status"])

            with pytest.raises(ValidationError) as excinfo:
                serializer.save()
            assert "reservation" in excinfo.value.detail

            reservation.refresh_from_db()
            assert reservation.status == Reservation.Status.CANCELLED  # untouched by the reject
            assert not Checkout.objects.filter(reservation_id=reservation.id).exists()


class TestWalkUpCheckoutVersusOtherUsersReservations:
    """Code-review finding (product decision: BLOCKING): a walk-up checkout
    (no `reservation` supplied) previously had NO check against other users'
    active reservations at all, defeating scheduling entirely. Now blocked
    against an OTHER user's active reservation covering `now`; the caller's
    OWN active reservation covering `now` never blocks their own walk-up; and
    no covering reservation at all is unaffected."""

    def test_walk_up_blocked_when_other_user_holds_active_reservation_covering_now(self, client):
        tenant = TenantFactory()
        holder = UserFactory(tenant=tenant)
        walkup_user = UserFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        with tenant_context(tenant.id):
            Reservation.objects.create(
                tenant=tenant,
                asset=asset,
                user=holder,
                start_at=timezone.now() - timedelta(minutes=5),
                end_at=timezone.now() + timedelta(hours=2),
                status=Reservation.Status.APPROVED,
            )

        _login(client, tenant, walkup_user)
        response = client.post(
            "/api/v1/checkouts/",
            data=json.dumps(_checkout_payload(asset)),
            content_type="application/json",
        )
        assert response.status_code == 400, response.content
        assert "reservation" in response.json()["errors"]

        with tenant_context(tenant.id):
            asset.refresh_from_db()
            assert asset.status == Asset.Status.AVAILABLE  # never checked out

    def test_walk_up_allowed_against_callers_own_active_reservation_covering_now(self, client):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        with tenant_context(tenant.id):
            Reservation.objects.create(
                tenant=tenant,
                asset=asset,
                user=member,
                start_at=timezone.now() - timedelta(minutes=5),
                end_at=timezone.now() + timedelta(hours=2),
                status=Reservation.Status.APPROVED,
            )

        _login(client, tenant, member)
        response = client.post(
            "/api/v1/checkouts/",
            data=json.dumps(_checkout_payload(asset)),
            content_type="application/json",
        )
        assert response.status_code == 201, response.content

    def test_walk_up_allowed_when_no_reservation_covers_now(self, client):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        other_member = UserFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        with tenant_context(tenant.id):
            # Another user's reservation exists, but its window is entirely
            # in the future -- it doesn't cover `now`, so it must not block.
            Reservation.objects.create(
                tenant=tenant,
                asset=asset,
                user=other_member,
                start_at=timezone.now() + timedelta(hours=3),
                end_at=timezone.now() + timedelta(hours=5),
                status=Reservation.Status.APPROVED,
            )

        _login(client, tenant, member)
        response = client.post(
            "/api/v1/checkouts/",
            data=json.dumps(_checkout_payload(asset)),
            content_type="application/json",
        )
        assert response.status_code == 201, response.content

    def test_approver_bypasses_walk_up_conflict_with_other_users_reservation(self, client):
        """Code-review finding, product decision: consistent with the
        `requires_approval` gate's existing approver bypass, someone holding
        `reservation.approve` in this asset's project scope may walk up and
        check out an asset even over another user's active reservation
        covering `now`."""
        tenant = TenantFactory()
        holder = UserFactory(tenant=tenant)
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)  # holds reservation.approve tenant-wide
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        with tenant_context(tenant.id):
            Reservation.objects.create(
                tenant=tenant,
                asset=asset,
                user=holder,
                start_at=timezone.now() - timedelta(minutes=5),
                end_at=timezone.now() + timedelta(hours=2),
                status=Reservation.Status.APPROVED,
            )

        _login(client, tenant, admin)
        response = client.post(
            "/api/v1/checkouts/",
            data=json.dumps(_checkout_payload(asset)),
            content_type="application/json",
        )
        assert response.status_code == 201, response.content

    def test_project_lead_approver_bypasses_walk_up_conflict_scoped_to_own_project(self, client):
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        holder = UserFactory(tenant=tenant)
        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, project, ROLE_PROJECT_LEAD)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        with tenant_context(tenant.id):
            asset.project = project
            asset.save(update_fields=["project"])
            Reservation.objects.create(
                tenant=tenant,
                asset=asset,
                user=holder,
                start_at=timezone.now() - timedelta(minutes=5),
                end_at=timezone.now() + timedelta(hours=2),
                status=Reservation.Status.APPROVED,
            )

        _login(client, tenant, lead)
        response = client.post(
            "/api/v1/checkouts/",
            data=json.dumps(_checkout_payload(asset)),
            content_type="application/json",
        )
        assert response.status_code == 201, response.content


class TestCheckoutFromCompletedReservation:
    """Product decision (bug fix): a `completed` reservation (checked out
    early, returned mid-window, `fulfilled` -> `completed`) may re-back a
    NEW checkout under that SAME reservation as long as `timezone.now()` is
    still inside its original `[start_at, end_at)` window -- avoiding a
    fresh approval cycle for an already-approved window."""

    def test_checkout_from_completed_reservation_succeeds_within_its_window(self, client):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        start = timezone.now() - timedelta(hours=1)
        end = timezone.now() + timedelta(hours=2)
        with tenant_context(tenant.id):
            reservation = Reservation.objects.create(
                tenant=tenant,
                asset=asset,
                user=member,
                start_at=start,
                end_at=end,
                status=Reservation.Status.COMPLETED,
            )

        _login(client, tenant, member)
        payload = _checkout_payload(asset)
        payload["reservation"] = reservation.id
        response = client.post(
            "/api/v1/checkouts/", data=json.dumps(payload), content_type="application/json"
        )
        assert response.status_code == 201, response.content
        assert response.json()["reservation"] == reservation.id

        with tenant_context(tenant.id):
            reservation.refresh_from_db()
            assert reservation.status == Reservation.Status.FULFILLED

    def test_checkout_from_completed_reservation_fails_once_its_window_has_passed(self, client):
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
                status=Reservation.Status.COMPLETED,
            )

        _login(client, tenant, member)
        payload = _checkout_payload(asset)
        payload["reservation"] = reservation.id
        response = client.post(
            "/api/v1/checkouts/", data=json.dumps(payload), content_type="application/json"
        )
        assert response.status_code == 400, response.content
        assert "reservation" in response.json()["errors"]

        with tenant_context(tenant.id):
            reservation.refresh_from_db()
            assert reservation.status == Reservation.Status.COMPLETED  # unchanged

    def test_reuse_of_completed_reservation_against_a_rebooked_window_gets_a_clean_409(self, client):
        """Code-review finding: while `completed`, a reservation is OUTSIDE
        `Reservation.ACTIVE_STATUSES` and its window is legitimately
        rebookable by someone else. If the original holder then tries to
        reuse it for a second checkout (still inside their own window), and
        that window now overlaps someone else's `approved` reservation,
        writing this row back to `fulfilled` collides with the `0002` GiST
        exclusion constraint. This must surface as the same clean
        `ReservationConflict` 409 `create_reservation` raises for an
        ordinary overlapping create -- never the generic `IntegrityError` ->
        "duplicate value" 409 fallback."""
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        other_member = UserFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        start = timezone.now() - timedelta(hours=1)
        end = timezone.now() + timedelta(hours=2)
        with tenant_context(tenant.id):
            reservation = Reservation.objects.create(
                tenant=tenant,
                asset=asset,
                user=member,
                start_at=start,
                end_at=end,
                status=Reservation.Status.COMPLETED,
            )
            # The window is free while `completed` -- another user can
            # legitimately book an overlapping slot.
            Reservation.objects.create(
                tenant=tenant,
                asset=asset,
                user=other_member,
                start_at=start + timedelta(minutes=30),
                end_at=end + timedelta(minutes=30),
                status=Reservation.Status.APPROVED,
            )

        _login(client, tenant, member)
        payload = _checkout_payload(asset)
        payload["reservation"] = reservation.id
        response = client.post(
            "/api/v1/checkouts/", data=json.dumps(payload), content_type="application/json"
        )
        assert response.status_code == 409, response.content
        detail = response.json().get("detail", "")
        assert "overlapping" in detail.lower()
        assert "duplicate" not in detail.lower()

        with tenant_context(tenant.id):
            reservation.refresh_from_db()
            assert reservation.status == Reservation.Status.COMPLETED  # unchanged, rolled back
            assert not Checkout.objects.filter(reservation=reservation).exists()
