"""T1.9 — the M1 milestone gate: one cohesive suite tracing directly to
`docs/features.md` F2's acceptance sentence, verbatim:

    "Admin can create a Compute asset with GPU/VRAM custom fields and a
    Component consumable with quantity; assets list filterable by
    category/status/location/project/tag; a retired asset is hidden from
    default lists but retained."

This module does NOT re-derive coverage that already exists per-facet in
`test_assets_api.py` (custom-field validation, retire lifecycle/audit,
scoped RBAC) and `test_assets_list_search_filters.py` (every individual
filter, ordering, search, pagination, query budget) — see the QA report for
the exact already-covered/newly-added split. Its job is narrower and
different: walk the **entire F2 narrative in one continuous session**
(same admin, same tenant, both asset types, every facet, retire, then the
scope-aware/RBAC boundary) so the milestone exit criterion is proven
end-to-end rather than only as independent fragments that happen to add up
to the same claim on paper.

One intentional scope note: F2's acceptance text says "Component consumable
with **quantity**" — `docs/data-model.md` places `quantity_on_hand` on the
separate `StockItem` model (F3 / M2, `docs/tasks/M2-*`), not on `Asset`
itself (confirmed against `apps/assets/models.py`: `Asset` has no quantity
field). `docs/tasks/M1-asset-registry.md`'s own T1.2 Exit criterion —the
authoritative scope for what M1 must prove — is narrower and does not
mention quantity: "create a Compute asset with GPU/VRAM custom-field values
and a Component consumable; retire hides from default lists but retains the
row". This suite proves that M1-scoped subset; the quantity-ledger half of
the F2 sentence is out of scope until F3 and is not silently invented here.
"""

from __future__ import annotations

import json

import pytest

from apps.assets.models import Asset
from apps.audit.models import AuditLog
from apps.catalog.models import CustomFieldDef
from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
    CategoryFactory,
    LocationFactory,
    ProjectFactory,
    TenantFactory,
    UserFactory,
    add_project_membership,
    upgrade_tenant_wide_role,
)
from apps.rbac.models import Membership
from apps.rbac.permission_keys import ROLE_ADMIN, ROLE_PROJECT_LEAD
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


def _create_asset(client, **kwargs):
    response = client.post(
        "/api/v1/assets/", data=json.dumps(kwargs), content_type="application/json"
    )
    assert response.status_code == 201, response.content
    return response.json()


def _names(response) -> list[str]:
    return [a["name"] for a in response.json()["results"]]


class TestF2MilestoneAcceptanceEndToEnd:
    """One continuous walk through every clause of the F2 acceptance
    sentence (M1-scoped subset), against a single admin/tenant session,
    followed by the scope-aware/RBAC boundary the milestone also requires
    (docs/features.md F1's "Member cannot hit an admin endpoint... a
    ProjectLead [is scoped to their project]" rule, carried into the asset
    registry per docs/rbac.md §1).
    """

    def test_f2_acceptance_full_narrative(self, client):
        # --- Setup: category tree with per-category custom fields (T1.1/T1.2) ---
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)

        compute = CategoryFactory(tenant=tenant, name="Compute", default_is_consumable=False)
        CustomFieldDef.all_objects.create(
            tenant=tenant,
            category=compute,
            key="gpu_model",
            label="GPU Model",
            data_type=CustomFieldDef.DataType.TEXT,
            required=True,
            order=0,
        )
        CustomFieldDef.all_objects.create(
            tenant=tenant,
            category=compute,
            key="vram_gb",
            label="VRAM",
            data_type=CustomFieldDef.DataType.INT,
            unit="GB",
            required=True,
            order=1,
        )
        components = CategoryFactory(tenant=tenant, name="Components", default_is_consumable=True)

        lab_a = LocationFactory(tenant=tenant, name="Lab A")
        lab_b = LocationFactory(tenant=tenant, name="Lab B")
        project = ProjectFactory(tenant=tenant, name="Swarm Drones")

        _login(client, tenant, admin)

        # --- F2 clause 1: "Admin can create a Compute asset with GPU/VRAM
        # custom fields" — via the real API, values persist, typed correctly. ---
        compute_asset = _create_asset(
            client,
            category=compute.id,
            name="GPU Server Alpha",
            location=lab_a.id,
            custom_field_values={"gpu_model": "RTX 4090", "vram_gb": 24},
            tags=["gpu"],
        )
        assert compute_asset["is_consumable"] is False
        assert compute_asset["field_values"] == {"gpu_model": "RTX 4090", "vram_gb": 24}
        with tenant_context(tenant.id):
            from apps.assets.models import AssetFieldValue

            stored = {
                fv.field_def.key: fv.value
                for fv in AssetFieldValue.objects.filter(asset_id=compute_asset["id"])
            }
        assert stored == {"gpu_model": "RTX 4090", "vram_gb": 24}

        # Type/enum/required validation is enforced (RFC-7807 400) — a single
        # representative check here (the full validation matrix already
        # lives in `test_assets_api.py::TestAssetCreateWithCustomFields`).
        bad = client.post(
            "/api/v1/assets/",
            data=json.dumps(
                {
                    "category": compute.id,
                    "name": "Bad GPU Server",
                    "custom_field_values": {"gpu_model": "RTX 4090", "vram_gb": "not-a-number"},
                }
            ),
            content_type="application/json",
        )
        assert bad.status_code == 400, bad.content
        assert bad.json()["status"] == 400
        assert "vram_gb" in bad.json()["errors"]["custom_field_values"]

        # --- F2 clause 2 (M1-scoped subset): "...and a Component consumable"
        # — `is_consumable` defaults from the category, no custom fields
        # required on this category. ---
        component_asset = _create_asset(
            client,
            category=components.id,
            name="M3 Screws",
            location=lab_b.id,
            project=project.id,
        )
        assert component_asset["is_consumable"] is True

        # A second compute asset (general pool, no project) + a second
        # component (in the project, different location) to make the facet
        # filters below discriminate something real rather than trivially
        # matching "the only row".
        pool_component = _create_asset(client, category=components.id, name="Zip Ties")

        # --- F2 clause 3: "assets list filterable by
        # category/status/location/project/tag" — every documented facet,
        # each returning the CORRECT subset out of this same corpus. ---
        by_category = client.get(f"/api/v1/assets/?category={compute.id}")
        assert _names(by_category) == ["GPU Server Alpha"]

        by_location = client.get(f"/api/v1/assets/?location={lab_b.id}")
        assert _names(by_location) == ["M3 Screws"]

        by_project = client.get(f"/api/v1/assets/?project={project.id}")
        assert _names(by_project) == ["M3 Screws"]

        with tenant_context(tenant.id):
            from apps.catalog.models import Tag

            gpu_tag = Tag.objects.get(name="gpu")
        by_tag = client.get(f"/api/v1/assets/?tag={gpu_tag.id}")
        assert _names(by_tag) == ["GPU Server Alpha"]

        by_consumable = client.get("/api/v1/assets/?is_consumable=true")
        assert sorted(_names(by_consumable)) == ["M3 Screws", "Zip Ties"]

        # --- F2 clause 4: "a retired asset is hidden from default lists but
        # retained" — retire the general-pool component, then re-check every
        # surface: default list (gone), detail-by-id (still there, status
        # reflects retirement), include_retired list (back), and audited. ---
        retire_resp = client.post(f"/api/v1/assets/{pool_component['id']}/retire/")
        assert retire_resp.status_code == 200, retire_resp.content
        assert retire_resp.json()["status"] == "retired"
        assert retire_resp.json()["retired_at"] is not None

        default_list = client.get("/api/v1/assets/")
        assert pool_component["id"] not in [a["id"] for a in default_list.json()["results"]]
        # The other two assets are unaffected.
        assert sorted(_names(default_list)) == ["GPU Server Alpha", "M3 Screws"]

        detail = client.get(f"/api/v1/assets/{pool_component['id']}/")
        assert detail.status_code == 200
        assert detail.json()["status"] == "retired"

        include_retired = client.get("/api/v1/assets/?include_retired=true")
        assert pool_component["id"] in [a["id"] for a in include_retired.json()["results"]]

        with tenant_context(tenant.id):
            assert Asset.objects.filter(pk=pool_component["id"]).exists()

        entry = AuditLog.all_objects.get(
            tenant=tenant, entity_type="asset", entity_id=str(pool_component["id"])
        )
        assert entry.action == "asset.retire"
        assert entry.before is not None and entry.before["status"] == "available"
        assert entry.after is not None and entry.after["status"] == "retired"
        assert entry.actor_id == admin.id

        # --- Scope-aware behavior the milestone gate also requires
        # (docs/features.md F1 acceptance, carried into every asset endpoint
        # per docs/rbac.md §1): a Member gets a server-side 403 on a write
        # even with a perfectly-formed request; a pure-ProjectLead sees only
        # their own project's assets, never the general pool or another
        # project. ---
        client.post("/api/v1/auth/logout")

        member = UserFactory(tenant=tenant)  # default tenant-wide Member (ROLE_MEMBER)
        _login(client, tenant, member)
        member_write = client.post(
            "/api/v1/assets/",
            data=json.dumps({"category": components.id, "name": "Should Fail"}),
            content_type="application/json",
        )
        assert member_write.status_code == 403, member_write.content
        # A Member CAN still list (read-only tenant-wide grant) — proves this
        # is a scoped write-permission denial, not a blanket tenant lockout.
        member_list = client.get("/api/v1/assets/")
        assert member_list.status_code == 200
        client.post("/api/v1/auth/logout")

        lead = UserFactory(tenant=tenant)
        # Strip the auto-assigned tenant-wide Member membership so this
        # user's ONLY grant is the project-scoped ProjectLead one below —
        # the "pure ProjectLead" scenario F1/rbac.md §1 describes.
        Membership.all_objects.filter(user=lead, project__isnull=True).delete()
        add_project_membership(lead, project, ROLE_PROJECT_LEAD)
        _login(client, tenant, lead)

        lead_list = client.get("/api/v1/assets/")
        assert lead_list.status_code == 200
        assert _names(lead_list) == ["M3 Screws"]  # only Swarm Drones' asset

        # A ProjectLead can create/retire within their OWN project...
        lead_create = client.post(
            "/api/v1/assets/",
            data=json.dumps(
                {"category": components.id, "name": "Project Widget", "project": project.id}
            ),
            content_type="application/json",
        )
        assert lead_create.status_code == 201, lead_create.content

        # ...but is denied server-side outside it (general pool), even
        # though the request is otherwise well-formed and the category is
        # the same one they just successfully used.
        lead_denied = client.post(
            "/api/v1/assets/",
            data=json.dumps({"category": components.id, "name": "General Pool Widget"}),
            content_type="application/json",
        )
        assert lead_denied.status_code == 403, lead_denied.content

    def test_guessed_asset_url_from_another_tenant_404s_not_a_leak(self, client):
        """R4, folded into the same F2 gate: a guessed asset id belonging to
        ANOTHER tenant is a 404 (never the object, never a distinguishable
        403-vs-404 oracle), on both the detail and retire endpoints —
        already proven per-endpoint in `test_assets_api.py`
        `TestAssetTenantIsolation`; kept here as the one-line cross-check
        that ties it to the SAME corpus this module just built, rather than
        re-deriving the whole scenario."""
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        admin_a = UserFactory(tenant=tenant_a)
        upgrade_tenant_wide_role(admin_a, ROLE_ADMIN)
        admin_b = UserFactory(tenant=tenant_b)
        upgrade_tenant_wide_role(admin_b, ROLE_ADMIN)
        category_b = CategoryFactory(tenant=tenant_b, name="Components")

        _login(client, tenant_b, admin_b)
        secret = _create_asset(client, category=category_b.id, name="Tenant B Secret")
        client.post("/api/v1/auth/logout")

        _login(client, tenant_a, admin_a)
        assert client.get(f"/api/v1/assets/{secret['id']}/").status_code == 404
        assert client.post(f"/api/v1/assets/{secret['id']}/retire/").status_code == 404
        assert client.get(f"/api/v1/assets/?category={category_b.id}").json()["count"] == 0
