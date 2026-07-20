"""T3.6 — F5 acceptance: checkout sets `Asset.status = in_use` + creates an
open `Checkout`; checkin records condition and frees the asset (idempotent);
overdue detection (`?overdue=true` and `is_overdue`); `checkout.override`
scoped RBAC + audit; a reservation cannot be hijacked by another user; and
cross-tenant isolation (R4).
"""

from __future__ import annotations

import json
from datetime import timedelta

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
