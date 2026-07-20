"""T3.6 — F4 acceptance at the API layer: approval routing
(`Category.requires_approval`), scoped `reservation.approve` RBAC (Admin
tenant-wide / ProjectLead project-scoped / general-pool Admin-only), the
per-user active-reservation cap, the calendar feed, audit coverage for
create/approve/reject/cancel, and cross-tenant isolation (R4).
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from django.utils import timezone

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
from apps.rbac.permission_keys import ROLE_ADMIN, ROLE_PROJECT_LEAD, ROLE_VIEWER
from apps.reservations.models import Reservation
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


def _window(hours_from_now: int = 1, duration_hours: int = 2):
    start = timezone.now() + timedelta(hours=hours_from_now)
    end = start + timedelta(hours=duration_hours)
    return start, end


def _create_payload(asset, start=None, end=None):
    start, end = (start, end) if start and end else _window()
    return {
        "asset": asset.id,
        "start_at": _iso(start),
        "end_at": _iso(end),
    }


class TestApprovalRouting:
    def test_auto_approve_category_yields_approved_immediately(self, client):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        category = CategoryFactory(tenant=tenant, requires_approval=False)
        asset = AssetFactory(tenant=tenant, category=category)

        _login(client, tenant, member)
        response = client.post(
            "/api/v1/reservations/",
            data=json.dumps(_create_payload(asset)),
            content_type="application/json",
        )
        assert response.status_code == 201, response.content
        assert response.json()["status"] == "approved"

    def test_requires_approval_category_yields_pending(self, client):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        category = CategoryFactory(tenant=tenant, requires_approval=True)
        asset = AssetFactory(tenant=tenant, category=category)

        _login(client, tenant, member)
        response = client.post(
            "/api/v1/reservations/",
            data=json.dumps(_create_payload(asset)),
            content_type="application/json",
        )
        assert response.status_code == 201, response.content
        assert response.json()["status"] == "pending"

    def test_create_writes_audit_log(self, client):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))

        _login(client, tenant, member)
        response = client.post(
            "/api/v1/reservations/",
            data=json.dumps(_create_payload(asset)),
            content_type="application/json",
        )
        assert response.status_code == 201, response.content
        reservation_id = response.json()["id"]

        entries = AuditLog.all_objects.filter(
            tenant_id=tenant.id, entity_type="reservation", entity_id=reservation_id
        )
        assert entries.count() == 1
        assert entries.first().action == "reservation.create"


class TestScopedApproval:
    def test_project_lead_can_approve_own_project_pending_reservation(self, client):
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, project, ROLE_PROJECT_LEAD)
        member = UserFactory(tenant=tenant)

        category = CategoryFactory(tenant=tenant, requires_approval=True)
        asset = AssetFactory(tenant=tenant, category=category)
        with tenant_context(tenant.id):
            asset.project = project
            asset.save(update_fields=["project"])

        _login(client, tenant, member)
        create_response = client.post(
            "/api/v1/reservations/",
            data=json.dumps(_create_payload(asset)),
            content_type="application/json",
        )
        assert create_response.status_code == 201, create_response.content
        reservation_id = create_response.json()["id"]
        client.post("/api/v1/auth/logout")

        _login(client, tenant, lead)
        approve_response = client.post(f"/api/v1/reservations/{reservation_id}/approve/")
        assert approve_response.status_code == 200, approve_response.content
        assert approve_response.json()["status"] == "approved"
        assert approve_response.json()["approver"] == lead.id

        entries = AuditLog.all_objects.filter(
            tenant_id=tenant.id, entity_type="reservation", entity_id=reservation_id
        ).order_by("created_at")
        assert [e.action for e in entries] == ["reservation.create", "reservation.approve"]

    def test_project_lead_cannot_approve_other_projects_reservation(self, client):
        tenant = TenantFactory()
        own_project = ProjectFactory(tenant=tenant)
        other_project = ProjectFactory(tenant=tenant)
        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, own_project, ROLE_PROJECT_LEAD)
        member = UserFactory(tenant=tenant)

        category = CategoryFactory(tenant=tenant, requires_approval=True)
        asset = AssetFactory(tenant=tenant, category=category)
        with tenant_context(tenant.id):
            asset.project = other_project
            asset.save(update_fields=["project"])

        _login(client, tenant, member)
        create_response = client.post(
            "/api/v1/reservations/",
            data=json.dumps(_create_payload(asset)),
            content_type="application/json",
        )
        reservation_id = create_response.json()["id"]
        client.post("/api/v1/auth/logout")

        _login(client, tenant, lead)
        approve_response = client.post(f"/api/v1/reservations/{reservation_id}/approve/")
        assert approve_response.status_code == 403, approve_response.content

    def test_member_without_approve_permission_gets_403_even_on_guessed_url(self, client):
        """F1/R-boundary: a plain Member holding no `reservation.approve`
        grant anywhere is denied server-side, not merely hidden in the UI —
        hitting the approve action directly (a 'guessed URL') still 403s."""
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        other_member = UserFactory(tenant=tenant)

        category = CategoryFactory(tenant=tenant, requires_approval=True)
        asset = AssetFactory(tenant=tenant, category=category)

        _login(client, tenant, other_member)
        create_response = client.post(
            "/api/v1/reservations/",
            data=json.dumps(_create_payload(asset)),
            content_type="application/json",
        )
        reservation_id = create_response.json()["id"]
        client.post("/api/v1/auth/logout")

        _login(client, tenant, member)
        approve_response = client.post(f"/api/v1/reservations/{reservation_id}/approve/")
        assert approve_response.status_code == 403, approve_response.content

        # The reservation is genuinely untouched — no privilege escalation
        # snuck through despite the 403.
        with tenant_context(tenant.id):
            reservation = Reservation.objects.get(pk=reservation_id)
            assert reservation.status == Reservation.Status.PENDING

    def test_general_pool_approval_requires_admin_project_lead_scope_does_not_reach_it(
        self, client
    ):
        """docs/rbac.md §4: general-pool (project=None) approval-required
        reservations are Admin-only — a ProjectLead's scope (even with a
        real project membership elsewhere) never reaches the general pool.
        """
        tenant = TenantFactory()
        project = ProjectFactory(tenant=tenant)
        lead = UserFactory(tenant=tenant)
        add_project_membership(lead, project, ROLE_PROJECT_LEAD)
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        member = UserFactory(tenant=tenant)

        category = CategoryFactory(tenant=tenant, requires_approval=True)
        asset = AssetFactory(tenant=tenant, category=category)  # project=None: general pool

        _login(client, tenant, member)
        create_response = client.post(
            "/api/v1/reservations/",
            data=json.dumps(_create_payload(asset)),
            content_type="application/json",
        )
        reservation_id = create_response.json()["id"]
        client.post("/api/v1/auth/logout")

        _login(client, tenant, lead)
        denied = client.post(f"/api/v1/reservations/{reservation_id}/approve/")
        assert denied.status_code == 403, denied.content
        client.post("/api/v1/auth/logout")

        _login(client, tenant, admin)
        allowed = client.post(f"/api/v1/reservations/{reservation_id}/approve/")
        assert allowed.status_code == 200, allowed.content

    def test_reject_writes_audit_log_and_frees_the_window(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        member = UserFactory(tenant=tenant)

        category = CategoryFactory(tenant=tenant, requires_approval=True)
        asset = AssetFactory(tenant=tenant, category=category)
        start, end = _window()

        _login(client, tenant, member)
        create_response = client.post(
            "/api/v1/reservations/",
            data=json.dumps(_create_payload(asset, start, end)),
            content_type="application/json",
        )
        reservation_id = create_response.json()["id"]
        client.post("/api/v1/auth/logout")

        _login(client, tenant, admin)
        reject_response = client.post(
            f"/api/v1/reservations/{reservation_id}/reject/",
            data=json.dumps({"note": "not available"}),
            content_type="application/json",
        )
        assert reject_response.status_code == 200, reject_response.content
        assert reject_response.json()["status"] == "rejected"

        entries = AuditLog.all_objects.filter(
            tenant_id=tenant.id, entity_type="reservation", entity_id=reservation_id
        )
        assert entries.filter(action="reservation.approve").count() == 1

        # The rejected window is free again — the SAME window can be
        # re-booked without conflict.
        client.post("/api/v1/auth/logout")
        _login(client, tenant, member)
        rebook_response = client.post(
            "/api/v1/reservations/",
            data=json.dumps(_create_payload(asset, start, end)),
            content_type="application/json",
        )
        assert rebook_response.status_code == 201, rebook_response.content

    def test_cancel_writes_audit_log(self, client):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))

        _login(client, tenant, member)
        create_response = client.post(
            "/api/v1/reservations/",
            data=json.dumps(_create_payload(asset)),
            content_type="application/json",
        )
        reservation_id = create_response.json()["id"]

        cancel_response = client.post(f"/api/v1/reservations/{reservation_id}/cancel/")
        assert cancel_response.status_code == 200, cancel_response.content
        assert cancel_response.json()["status"] == "cancelled"

        entries = AuditLog.all_objects.filter(
            tenant_id=tenant.id, entity_type="reservation", entity_id=reservation_id
        )
        assert entries.filter(action="reservation.cancel").count() == 1


class TestPerUserReservationLimit:
    def test_per_user_active_reservation_cap_is_enforced(self, client, settings):
        """Uses the actual configured default
        (`RESERVATION_MAX_ACTIVE_PER_USER`, `config/settings/base.py`) rather
        than hard-coding 3, so this test tracks the real limit even if the
        documented Q10 default changes."""
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        limit = settings.RESERVATION_MAX_ACTIVE_PER_USER

        _login(client, tenant, member)
        for _i in range(limit):
            asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
            response = client.post(
                "/api/v1/reservations/",
                data=json.dumps(_create_payload(asset)),
                content_type="application/json",
            )
            assert response.status_code == 201, response.content

        # The (limit + 1)-th active reservation is rejected.
        extra_asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        over_limit_response = client.post(
            "/api/v1/reservations/",
            data=json.dumps(_create_payload(extra_asset)),
            content_type="application/json",
        )
        assert over_limit_response.status_code == 400, over_limit_response.content

    def test_cancelling_one_frees_a_slot_under_the_cap(self, client, settings):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        limit = settings.RESERVATION_MAX_ACTIVE_PER_USER

        _login(client, tenant, member)
        first_id = None
        for _i in range(limit):
            asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
            response = client.post(
                "/api/v1/reservations/",
                data=json.dumps(_create_payload(asset)),
                content_type="application/json",
            )
            assert response.status_code == 201, response.content
            if first_id is None:
                first_id = response.json()["id"]

        client.post(f"/api/v1/reservations/{first_id}/cancel/")

        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        response = client.post(
            "/api/v1/reservations/",
            data=json.dumps(_create_payload(asset)),
            content_type="application/json",
        )
        assert response.status_code == 201, response.content


class TestCalendarFeed:
    def test_approved_windows_appear_on_the_calendar_feed(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        start, end = _window(hours_from_now=5, duration_hours=1)

        _login(client, tenant, admin)
        create_response = client.post(
            "/api/v1/reservations/",
            data=json.dumps(_create_payload(asset, start, end)),
            content_type="application/json",
        )
        assert create_response.status_code == 201, create_response.content
        reservation_id = create_response.json()["id"]

        feed_from = start - timedelta(hours=1)
        feed_to = end + timedelta(hours=1)
        response = client.get(
            "/api/v1/reservations/", {"from": _iso(feed_from), "to": _iso(feed_to)}
        )
        assert response.status_code == 200, response.content
        ids = {row["id"] for row in response.json()["results"]}
        assert reservation_id in ids

    def test_calendar_feed_excludes_windows_outside_the_range(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))
        start, end = _window(hours_from_now=100, duration_hours=1)  # far in the future

        _login(client, tenant, admin)
        create_response = client.post(
            "/api/v1/reservations/",
            data=json.dumps(_create_payload(asset, start, end)),
            content_type="application/json",
        )
        assert create_response.status_code == 201, create_response.content
        reservation_id = create_response.json()["id"]

        # A narrow near-term window that does not overlap the far-future booking.
        response = client.get(
            "/api/v1/reservations/",
            {"from": _iso(timezone.now()), "to": _iso(timezone.now() + timedelta(hours=1))},
        )
        assert response.status_code == 200, response.content
        ids = {row["id"] for row in response.json()["results"]}
        assert reservation_id not in ids


class TestCrossTenantIsolation:
    def test_reservation_in_another_tenant_404s_for_a_guessed_id(self, client):
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
                start_at=timezone.now() + timedelta(hours=1),
                end_at=timezone.now() + timedelta(hours=2),
                status=Reservation.Status.APPROVED,
            )

        _login(client, tenant, admin)
        response = client.get(f"/api/v1/reservations/{other_reservation.id}/")
        assert response.status_code == 404, response.content

        # Same for the mutating actions — a guessed cross-tenant id must
        # never let an approve/reject/cancel reach another tenant's row.
        for action in ("approve", "reject", "cancel"):
            action_response = client.post(f"/api/v1/reservations/{other_reservation.id}/{action}/")
            assert action_response.status_code == 404, (action, action_response.content)

    def test_list_never_shows_another_tenants_reservations(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)

        other_tenant = TenantFactory()
        other_category = CategoryFactory(tenant=other_tenant)
        other_asset = AssetFactory(tenant=other_tenant, category=other_category)
        other_user = UserFactory(tenant=other_tenant)
        with tenant_context(other_tenant.id):
            Reservation.objects.create(
                tenant=other_tenant,
                asset=other_asset,
                user=other_user,
                start_at=timezone.now() + timedelta(hours=1),
                end_at=timezone.now() + timedelta(hours=2),
                status=Reservation.Status.APPROVED,
            )

        _login(client, tenant, admin)
        response = client.get("/api/v1/reservations/")
        assert response.status_code == 200, response.content
        assert response.json()["results"] == []

    def test_project_leads_of_different_tenants_cannot_approve_across_tenants(self, client):
        """Belt-and-suspenders R4 + RBAC combination (mirrors
        `apps.rbac.tests.test_rbac_scope`'s cross-tenant ProjectLead test):
        a same-named-by-coincidence project in a DIFFERENT tenant never
        grants scope."""
        tenant_a = TenantFactory()
        project_a = ProjectFactory(tenant=tenant_a)
        lead_a = UserFactory(tenant=tenant_a)
        add_project_membership(lead_a, project_a, ROLE_PROJECT_LEAD)

        tenant_b = TenantFactory()
        project_b = ProjectFactory(tenant=tenant_b)
        category_b = CategoryFactory(tenant=tenant_b, requires_approval=True)
        asset_b = AssetFactory(tenant=tenant_b, category=category_b)
        with tenant_context(tenant_b.id):
            asset_b.project = project_b
            asset_b.save(update_fields=["project"])
        member_b = UserFactory(tenant=tenant_b)

        _login(client, tenant_b, member_b)
        create_response = client.post(
            "/api/v1/reservations/",
            data=json.dumps(_create_payload(asset_b)),
            content_type="application/json",
        )
        assert create_response.status_code == 201, create_response.content
        reservation_id = create_response.json()["id"]
        client.post("/api/v1/auth/logout")

        # lead_a authenticates against tenant_a's login, so this reservation
        # id (belonging to tenant_b) is invisible to them entirely — R4.
        _login(client, tenant_a, lead_a)
        response = client.post(f"/api/v1/reservations/{reservation_id}/approve/")
        assert response.status_code in (403, 404), response.content


class TestViewerCannotCreate:
    def test_viewer_cannot_create_a_reservation(self, client):
        tenant = TenantFactory()
        viewer = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(viewer, ROLE_VIEWER)
        asset = AssetFactory(tenant=tenant, category=CategoryFactory(tenant=tenant))

        _login(client, tenant, viewer)
        response = client.post(
            "/api/v1/reservations/",
            data=json.dumps(_create_payload(asset)),
            content_type="application/json",
        )
        assert response.status_code == 403, response.content
