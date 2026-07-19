"""T1.1 — catalog endpoints: tenant scoping + RBAC (add-endpoint golden path).

Covers, per the skill checklist:
- cross-tenant: a guessed id / another tenant's rows never leak (404, not the
  object; list scoped to zero);
- RBAC: Member/Viewer get a server-side 403 on category/location writes,
  Admin is allowed;
- the `/categories/{id}/fields` sub-resource, including tenant-scoping of a
  guessed category id;
- the R4/F1 carried finding: `Project.lead_user` is only selectable from the
  caller's own tenant.

Real HTTP requests via `client` (pytest-django's `Client`), going through the
full middleware stack (session -> `CurrentTenantMiddleware` -> RBAC), not a
bare view/permission-class call — this is the "integration, real session"
tier `apps.tenancy.tests.test_tenant_isolation` uses for the same reason.
"""

from __future__ import annotations

import json

import pytest

from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
    CategoryFactory,
    CustomFieldDefFactory,
    LocationFactory,
    TenantFactory,
    UserFactory,
    upgrade_tenant_wide_role,
)
from apps.rbac.permission_keys import ROLE_ADMIN

pytestmark = pytest.mark.django_db


def _login(client, tenant, user):
    response = client.post(
        "/api/v1/auth/login",
        {"tenant": tenant.slug, "email": user.email, "password": DEFAULT_TEST_PASSWORD},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return response


class TestCategoryTenantIsolation:
    def test_list_never_shows_another_tenants_categories(self, client):
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        admin_a = UserFactory(tenant=tenant_a)
        upgrade_tenant_wide_role(admin_a, ROLE_ADMIN)
        CategoryFactory(tenant=tenant_a, name="Compute")
        CategoryFactory(tenant=tenant_b, name="Compute")  # same name, other tenant

        _login(client, tenant_a, admin_a)
        response = client.get("/api/v1/categories/")

        assert response.status_code == 200
        names_and_tenants = [(r["id"], r["name"]) for r in response.json()["results"]]
        assert len(names_and_tenants) == 1

    def test_guessed_category_id_from_another_tenant_404s(self, client):
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        admin_a = UserFactory(tenant=tenant_a)
        upgrade_tenant_wide_role(admin_a, ROLE_ADMIN)
        other_category = CategoryFactory(tenant=tenant_b, name="Secret Category")

        _login(client, tenant_a, admin_a)
        response = client.get(f"/api/v1/categories/{other_category.id}/")

        assert response.status_code == 404

    def test_guessed_category_id_fields_subresource_404s(self, client):
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        admin_a = UserFactory(tenant=tenant_a)
        upgrade_tenant_wide_role(admin_a, ROLE_ADMIN)
        other_category = CategoryFactory(tenant=tenant_b, name="Secret Category")

        _login(client, tenant_a, admin_a)
        response = client.get(f"/api/v1/categories/{other_category.id}/fields/")

        assert response.status_code == 404


class TestCategoryRBAC:
    def test_admin_can_create_category_tree_and_field_defs(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        _login(client, tenant, admin)

        parent_resp = client.post(
            "/api/v1/categories/",
            data=json.dumps({"name": "Compute", "requires_approval": True}),
            content_type="application/json",
        )
        assert parent_resp.status_code == 201, parent_resp.content
        parent_id = parent_resp.json()["id"]

        child_resp = client.post(
            "/api/v1/categories/",
            data=json.dumps({"name": "GPU Servers", "parent": parent_id}),
            content_type="application/json",
        )
        assert child_resp.status_code == 201
        assert child_resp.json()["parent"] == parent_id

        field_resp = client.post(
            f"/api/v1/categories/{parent_id}/fields/",
            data=json.dumps(
                {
                    "key": "vram_gb",
                    "label": "VRAM (GB)",
                    "data_type": "int",
                    "unit": "GB",
                    "required": True,
                }
            ),
            content_type="application/json",
        )
        assert field_resp.status_code == 201, field_resp.content
        assert field_resp.json()["category"] == parent_id

        list_resp = client.get(f"/api/v1/categories/{parent_id}/fields/")
        assert list_resp.status_code == 200
        assert [f["key"] for f in list_resp.json()] == ["vram_gb"]

    def test_member_gets_server_side_403_on_category_create(self, client):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)  # default: tenant-wide Member
        _login(client, tenant, member)

        response = client.post(
            "/api/v1/categories/",
            data=json.dumps({"name": "Should Fail"}),
            content_type="application/json",
        )
        assert response.status_code == 403

    def test_member_can_still_read_categories(self, client):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        CategoryFactory(tenant=tenant, name="Compute")
        _login(client, tenant, member)

        response = client.get("/api/v1/categories/")
        assert response.status_code == 200
        assert response.json()["count"] == 1

    def test_viewer_gets_403_on_category_create(self, client):
        tenant = TenantFactory()
        viewer = UserFactory(tenant=tenant)
        from apps.rbac.permission_keys import ROLE_VIEWER

        upgrade_tenant_wide_role(viewer, ROLE_VIEWER)
        _login(client, tenant, viewer)

        response = client.post(
            "/api/v1/categories/",
            data=json.dumps({"name": "Should Fail"}),
            content_type="application/json",
        )
        assert response.status_code == 403

    def test_member_gets_403_on_fields_subresource_write(self, client):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        category = CategoryFactory(tenant=tenant, name="Compute")
        _login(client, tenant, member)

        response = client.post(
            f"/api/v1/categories/{category.id}/fields/",
            data=json.dumps({"key": "vram_gb", "label": "VRAM", "data_type": "int"}),
            content_type="application/json",
        )
        assert response.status_code == 403


class TestLocationRBACAndScoping:
    def test_admin_can_build_location_tree(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        _login(client, tenant, admin)

        room = client.post(
            "/api/v1/locations/",
            data=json.dumps({"name": "Main Lab", "kind": "room"}),
            content_type="application/json",
        )
        assert room.status_code == 201
        room_id = room.json()["id"]

        shelf = client.post(
            "/api/v1/locations/",
            data=json.dumps({"name": "Shelf A", "kind": "shelf", "parent": room_id}),
            content_type="application/json",
        )
        assert shelf.status_code == 201
        assert shelf.json()["parent"] == room_id

    def test_member_gets_403_on_location_create(self, client):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        _login(client, tenant, member)

        response = client.post(
            "/api/v1/locations/",
            data=json.dumps({"name": "Should Fail"}),
            content_type="application/json",
        )
        assert response.status_code == 403

    def test_list_never_shows_another_tenants_locations(self, client):
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        admin_a = UserFactory(tenant=tenant_a)
        upgrade_tenant_wide_role(admin_a, ROLE_ADMIN)
        LocationFactory(tenant=tenant_a, name="Main Lab")
        LocationFactory(tenant=tenant_b, name="Main Lab")

        _login(client, tenant_a, admin_a)
        response = client.get("/api/v1/locations/")

        assert response.status_code == 200
        assert response.json()["count"] == 1


class TestProjectLeadUserTenantScoping:
    """R4/F1 carried finding: `Project.lead_user` must only be selectable
    from the caller's own tenant, never any other tenant's `User` row."""

    def test_lead_user_from_own_tenant_is_accepted(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        colleague = UserFactory(tenant=tenant)
        _login(client, tenant, admin)

        response = client.post(
            "/api/v1/projects/",
            data=json.dumps({"name": "Project Alpha", "lead_user": colleague.id}),
            content_type="application/json",
        )
        assert response.status_code == 201, response.content
        assert response.json()["lead_user"] == colleague.id

    def test_lead_user_from_another_tenant_is_rejected(self, client):
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        admin_a = UserFactory(tenant=tenant_a)
        upgrade_tenant_wide_role(admin_a, ROLE_ADMIN)
        other_tenant_user = UserFactory(tenant=tenant_b)
        _login(client, tenant_a, admin_a)

        response = client.post(
            "/api/v1/projects/",
            data=json.dumps({"name": "Project Alpha", "lead_user": other_tenant_user.id}),
            content_type="application/json",
        )
        assert response.status_code == 400
        assert "lead_user" in response.json()["errors"]

    def test_member_gets_403_on_project_create(self, client):
        """ASSUMPTION (flagged): Project writes are gated by `tenant.manage`
        (Admin-only) — see `apps.catalog.permissions` module docstring."""
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        _login(client, tenant, member)

        response = client.post(
            "/api/v1/projects/",
            data=json.dumps({"name": "Should Fail"}),
            content_type="application/json",
        )
        assert response.status_code == 403


class TestTagReadOnly:
    def test_tags_list_is_tenant_scoped(self, client):
        from apps.common.tests.factories import TagFactory

        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        member_a = UserFactory(tenant=tenant_a)
        TagFactory(tenant=tenant_a, name="drone")
        TagFactory(tenant=tenant_b, name="drone")

        _login(client, tenant_a, member_a)
        response = client.get("/api/v1/tags/")

        assert response.status_code == 200
        assert response.json()["count"] == 1

    def test_tags_endpoint_has_no_write_verb(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        _login(client, tenant, admin)

        response = client.post(
            "/api/v1/tags/", data=json.dumps({"name": "new-tag"}), content_type="application/json"
        )
        assert response.status_code in (403, 405)


class TestQueryBudget:
    def test_category_list_query_count_is_bounded(self, client, django_assert_max_num_queries):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        parent = CategoryFactory(tenant=tenant, name="Compute")
        for i in range(10):
            CategoryFactory(tenant=tenant, name=f"Compute Child {i}", parent=parent)

        _login(client, tenant, admin)
        # Bounded regardless of row count: permission check + count + page of
        # rows (select_related parent, prefetch field_defs) — no N+1 per row.
        with django_assert_max_num_queries(15):
            response = client.get("/api/v1/categories/")
        assert response.status_code == 200
        assert response.json()["count"] == 11


class TestDbConstraintsMapToRFC7807:
    """Code-review follow-up: `tenant` isn't a serializer field, so DRF adds
    no `UniqueTogetherValidator` for free — a duplicate name/key previously
    hit the DB `UniqueConstraint` and raised `IntegrityError` -> an unhandled
    HTML 500, breaking the "every error is RFC-7807 problem+json" convention.
    Same story for deleting a `PROTECT`-referenced parent (`ProtectedError`).
    Now: duplicates -> clean 400 with the offending field (serializer-level
    validation, `apps.catalog.serializers`); `ProtectedError` on delete ->
    409 (centralized in `apps.common.errors.rfc7807_exception_handler`, so
    T1.2's `Asset` endpoints inherit the same behavior for free).
    """

    def test_duplicate_category_name_under_same_parent_is_400_not_500(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        CategoryFactory(tenant=tenant, name="Compute", parent=None)
        _login(client, tenant, admin)

        response = client.post(
            "/api/v1/categories/",
            data=json.dumps({"name": "Compute"}),
            content_type="application/json",
        )

        assert response.status_code == 400
        body = response.json()
        assert body["type"] == "about:blank"
        assert body["status"] == 400
        assert "name" in body["errors"]

    def test_duplicate_category_name_under_different_parents_is_allowed(self, client):
        """Sanity: the uniqueness check is scoped by `parent`, not global —
        the same name under a DIFFERENT parent is fine."""
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        parent_a = CategoryFactory(tenant=tenant, name="Parent A")
        parent_b = CategoryFactory(tenant=tenant, name="Parent B")
        CategoryFactory(tenant=tenant, name="Shelf", parent=parent_a)
        _login(client, tenant, admin)

        response = client.post(
            "/api/v1/categories/",
            data=json.dumps({"name": "Shelf", "parent": parent_b.id}),
            content_type="application/json",
        )
        assert response.status_code == 201, response.content

    def test_duplicate_custom_field_key_under_same_category_is_400_not_500(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = CategoryFactory(tenant=tenant, name="Compute")
        _login(client, tenant, admin)

        first = client.post(
            f"/api/v1/categories/{category.id}/fields/",
            data=json.dumps({"key": "vram_gb", "label": "VRAM", "data_type": "int"}),
            content_type="application/json",
        )
        assert first.status_code == 201, first.content

        second = client.post(
            f"/api/v1/categories/{category.id}/fields/",
            data=json.dumps({"key": "vram_gb", "label": "VRAM (dup)", "data_type": "int"}),
            content_type="application/json",
        )

        assert second.status_code == 400
        body = second.json()
        assert body["type"] == "about:blank"
        assert body["status"] == 400
        assert "key" in body["errors"]

    def test_duplicate_location_name_under_same_parent_is_400_not_500(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        LocationFactory(tenant=tenant, name="Main Lab", parent=None)
        _login(client, tenant, admin)

        response = client.post(
            "/api/v1/locations/",
            data=json.dumps({"name": "Main Lab"}),
            content_type="application/json",
        )

        assert response.status_code == 400
        assert "name" in response.json()["errors"]

    def test_duplicate_project_name_is_400_not_500(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        _login(client, tenant, admin)

        first = client.post(
            "/api/v1/projects/",
            data=json.dumps({"name": "Project Alpha"}),
            content_type="application/json",
        )
        assert first.status_code == 201

        second = client.post(
            "/api/v1/projects/",
            data=json.dumps({"name": "Project Alpha"}),
            content_type="application/json",
        )
        assert second.status_code == 400
        assert "name" in second.json()["errors"]

    def test_deleting_protected_parent_category_is_409_not_500(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        parent = CategoryFactory(tenant=tenant, name="Compute")
        CategoryFactory(tenant=tenant, name="GPU Servers", parent=parent)
        _login(client, tenant, admin)

        response = client.delete(f"/api/v1/categories/{parent.id}/")

        assert response.status_code == 409, response.content
        body = response.json()
        assert body["type"] == "about:blank"
        assert body["status"] == 409

        # The parent must still exist -- the delete was rejected, not
        # partially applied.
        assert client.get(f"/api/v1/categories/{parent.id}/").status_code == 200

    def test_deleting_protected_parent_location_is_409_not_500(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        room = LocationFactory(tenant=tenant, name="Main Lab")
        LocationFactory(tenant=tenant, name="Shelf A", parent=room)
        _login(client, tenant, admin)

        response = client.delete(f"/api/v1/locations/{room.id}/")

        assert response.status_code == 409, response.content
        assert response.json()["status"] == 409

    def test_deleting_childless_category_still_works(self, client):
        """Sanity: the `ProtectedError` handling doesn't turn EVERY delete
        into a 409 — a leaf category (no children) deletes fine."""
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        leaf = CategoryFactory(tenant=tenant, name="Leaf")
        _login(client, tenant, admin)

        response = client.delete(f"/api/v1/categories/{leaf.id}/")
        assert response.status_code == 204


class TestTreeCycleDetection:
    """Code-review finding: only direct self-parenting (`A.parent = A`) was
    blocked; a longer cycle (`A.parent = B` then `B.parent = A`) was not,
    which would infinite-loop any recursive tree/breadcrumb traversal (T1.5).
    """

    def test_two_node_cycle_is_rejected_for_category(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        node_a = CategoryFactory(tenant=tenant, name="A")
        node_b = CategoryFactory(tenant=tenant, name="B", parent=node_a)  # A <- B
        _login(client, tenant, admin)

        # Attempt B -> A (i.e. make A's parent B), closing the cycle.
        response = client.patch(
            f"/api/v1/categories/{node_a.id}/",
            data=json.dumps({"parent": node_b.id}),
            content_type="application/json",
        )

        assert response.status_code == 400
        assert "parent" in response.json()["errors"]

        # The tree must be unchanged.
        node_a.refresh_from_db()
        assert node_a.parent_id is None

    def test_three_node_cycle_is_rejected_for_category(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        node_a = CategoryFactory(tenant=tenant, name="A")
        node_b = CategoryFactory(tenant=tenant, name="B", parent=node_a)
        node_c = CategoryFactory(tenant=tenant, name="C", parent=node_b)
        _login(client, tenant, admin)

        # Attempt to close the loop: A's parent = C (A <- B <- C <- A).
        response = client.patch(
            f"/api/v1/categories/{node_a.id}/",
            data=json.dumps({"parent": node_c.id}),
            content_type="application/json",
        )

        assert response.status_code == 400
        assert "parent" in response.json()["errors"]

    def test_two_node_cycle_is_rejected_for_location(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        node_a = LocationFactory(tenant=tenant, name="A")
        node_b = LocationFactory(tenant=tenant, name="B", parent=node_a)
        _login(client, tenant, admin)

        response = client.patch(
            f"/api/v1/locations/{node_a.id}/",
            data=json.dumps({"parent": node_b.id}),
            content_type="application/json",
        )

        assert response.status_code == 400
        assert "parent" in response.json()["errors"]

        # Sanity: re-parenting to something that ISN'T an ancestor still works.
        node_c = LocationFactory(tenant=tenant, name="C")
        follow_up = client.patch(
            f"/api/v1/locations/{node_a.id}/",
            data=json.dumps({"parent": node_c.id}),
            content_type="application/json",
        )
        assert follow_up.status_code == 200


class TestCustomFieldDefEditDeleteReorder:
    """M1 follow-up: `PATCH`/`DELETE /categories/{cat_id}/fields/{field_id}`
    and `POST /categories/{id}/fields/reorder` (docs/api-and-ui.md).
    """

    def test_admin_can_edit_a_field_def(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = CategoryFactory(tenant=tenant, name="Compute")
        field = CustomFieldDefFactory(
            tenant=tenant, category=category, key="vram", label="VRAM", data_type="int"
        )
        _login(client, tenant, admin)

        response = client.patch(
            f"/api/v1/categories/{category.id}/fields/{field.id}/",
            data=json.dumps({"label": "VRAM (GB)", "unit": "GB", "required": True}),
            content_type="application/json",
        )

        assert response.status_code == 200, response.content
        body = response.json()
        assert body["label"] == "VRAM (GB)"
        assert body["unit"] == "GB"
        assert body["required"] is True
        field.refresh_from_db()
        assert field.label == "VRAM (GB)"
        assert field.unit == "GB"
        assert field.required is True

    def test_edit_duplicate_key_under_same_category_is_400(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = CategoryFactory(tenant=tenant, name="Compute")
        CustomFieldDefFactory(tenant=tenant, category=category, key="vram", data_type="int")
        other = CustomFieldDefFactory(tenant=tenant, category=category, key="ram", data_type="int")
        _login(client, tenant, admin)

        response = client.patch(
            f"/api/v1/categories/{category.id}/fields/{other.id}/",
            data=json.dumps({"key": "vram"}),
            content_type="application/json",
        )

        assert response.status_code == 400
        assert "key" in response.json()["errors"]

    def test_admin_can_delete_a_field_def(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = CategoryFactory(tenant=tenant, name="Compute")
        field = CustomFieldDefFactory(tenant=tenant, category=category, key="vram")
        _login(client, tenant, admin)

        response = client.delete(f"/api/v1/categories/{category.id}/fields/{field.id}/")

        assert response.status_code == 204
        list_resp = client.get(f"/api/v1/categories/{category.id}/fields/")
        assert list_resp.json() == []

    def test_reorder_persists_new_order(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = CategoryFactory(tenant=tenant, name="Compute")
        first = CustomFieldDefFactory(tenant=tenant, category=category, key="a", order=0)
        second = CustomFieldDefFactory(tenant=tenant, category=category, key="b", order=1)
        third = CustomFieldDefFactory(tenant=tenant, category=category, key="c", order=2)
        _login(client, tenant, admin)

        response = client.post(
            f"/api/v1/categories/{category.id}/fields/reorder/",
            data=json.dumps({"order": [third.id, first.id, second.id]}),
            content_type="application/json",
        )

        assert response.status_code == 200, response.content
        assert [f["key"] for f in response.json()] == ["c", "a", "b"]

        list_resp = client.get(f"/api/v1/categories/{category.id}/fields/")
        assert [f["key"] for f in list_resp.json()] == ["c", "a", "b"]

    def test_reorder_rejects_id_set_mismatch(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = CategoryFactory(tenant=tenant, name="Compute")
        first = CustomFieldDefFactory(tenant=tenant, category=category, key="a", order=0)
        CustomFieldDefFactory(tenant=tenant, category=category, key="b", order=1)
        _login(client, tenant, admin)

        # Missing the second field's id.
        response = client.post(
            f"/api/v1/categories/{category.id}/fields/reorder/",
            data=json.dumps({"order": [first.id]}),
            content_type="application/json",
        )

        assert response.status_code == 400
        assert "order" in response.json()["errors"]

    def test_reorder_rejects_id_from_another_category(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = CategoryFactory(tenant=tenant, name="Compute")
        other_category = CategoryFactory(tenant=tenant, name="Edge")
        first = CustomFieldDefFactory(tenant=tenant, category=category, key="a", order=0)
        foreign_field = CustomFieldDefFactory(
            tenant=tenant, category=other_category, key="x", order=0
        )
        _login(client, tenant, admin)

        response = client.post(
            f"/api/v1/categories/{category.id}/fields/reorder/",
            data=json.dumps({"order": [foreign_field.id, first.id]}),
            content_type="application/json",
        )

        assert response.status_code == 400
        assert "order" in response.json()["errors"]

    def test_cross_tenant_field_id_404s_on_edit_and_delete(self, client):
        tenant_a = TenantFactory()
        tenant_b = TenantFactory()
        admin_a = UserFactory(tenant=tenant_a)
        upgrade_tenant_wide_role(admin_a, ROLE_ADMIN)
        category_a = CategoryFactory(tenant=tenant_a, name="Compute")
        category_b = CategoryFactory(tenant=tenant_b, name="Compute")
        other_field = CustomFieldDefFactory(tenant=tenant_b, category=category_b, key="secret")
        _login(client, tenant_a, admin_a)

        edit_resp = client.patch(
            f"/api/v1/categories/{category_a.id}/fields/{other_field.id}/",
            data=json.dumps({"label": "Hacked"}),
            content_type="application/json",
        )
        assert edit_resp.status_code == 404

        delete_resp = client.delete(f"/api/v1/categories/{category_a.id}/fields/{other_field.id}/")
        assert delete_resp.status_code == 404

    def test_cross_category_field_id_404s_even_in_same_tenant(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category_a = CategoryFactory(tenant=tenant, name="Compute")
        category_b = CategoryFactory(tenant=tenant, name="Edge")
        field_in_b = CustomFieldDefFactory(tenant=tenant, category=category_b, key="vram")
        _login(client, tenant, admin)

        # Right tenant, but the URL's cat_id (`category_a`) is NOT this
        # field's category (it belongs to `category_b`) -- must 404, not
        # silently operate on a field from a sibling category.
        response = client.patch(
            f"/api/v1/categories/{category_a.id}/fields/{field_in_b.id}/",
            data=json.dumps({"label": "Hacked"}),
            content_type="application/json",
        )

        assert response.status_code == 404

    def test_member_gets_403_on_edit_delete_and_reorder(self, client):
        tenant = TenantFactory()
        member = UserFactory(tenant=tenant)
        category = CategoryFactory(tenant=tenant, name="Compute")
        field = CustomFieldDefFactory(tenant=tenant, category=category, key="vram")
        _login(client, tenant, member)

        edit_resp = client.patch(
            f"/api/v1/categories/{category.id}/fields/{field.id}/",
            data=json.dumps({"label": "Hacked"}),
            content_type="application/json",
        )
        assert edit_resp.status_code == 403

        delete_resp = client.delete(f"/api/v1/categories/{category.id}/fields/{field.id}/")
        assert delete_resp.status_code == 403

        reorder_resp = client.post(
            f"/api/v1/categories/{category.id}/fields/reorder/",
            data=json.dumps({"order": [field.id]}),
            content_type="application/json",
        )
        assert reorder_resp.status_code == 403

    def test_viewer_gets_403_on_edit(self, client):
        from apps.rbac.permission_keys import ROLE_VIEWER

        tenant = TenantFactory()
        viewer = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(viewer, ROLE_VIEWER)
        category = CategoryFactory(tenant=tenant, name="Compute")
        field = CustomFieldDefFactory(tenant=tenant, category=category, key="vram")
        _login(client, tenant, viewer)

        response = client.patch(
            f"/api/v1/categories/{category.id}/fields/{field.id}/",
            data=json.dumps({"label": "Hacked"}),
            content_type="application/json",
        )
        assert response.status_code == 403


class TestCustomFieldDefDataTypeChangePolicy:
    """Data-type-change policy (docs/api-and-ui.md): blocked once the field
    already has stored `AssetFieldValue` rows; deleting the field def cascades
    those values away instead of being blocked.
    """

    def _create_asset_with_field_value(self, client, category, field):
        response = client.post(
            "/api/v1/assets/",
            data=json.dumps(
                {
                    "category": category.id,
                    "name": "GPU Box",
                    "custom_field_values": {field.key: 32},
                }
            ),
            content_type="application/json",
        )
        assert response.status_code == 201, response.content
        return response.json()["id"]

    def test_data_type_change_blocked_once_field_is_in_use(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = CategoryFactory(tenant=tenant, name="Compute")
        field = CustomFieldDefFactory(tenant=tenant, category=category, key="vram", data_type="int")
        _login(client, tenant, admin)
        self._create_asset_with_field_value(client, category, field)

        response = client.patch(
            f"/api/v1/categories/{category.id}/fields/{field.id}/",
            data=json.dumps({"data_type": "text"}),
            content_type="application/json",
        )

        assert response.status_code == 400
        assert "data_type" in response.json()["errors"]
        field.refresh_from_db()
        assert field.data_type == "int"

    def test_data_type_change_allowed_when_not_in_use(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = CategoryFactory(tenant=tenant, name="Compute")
        field = CustomFieldDefFactory(tenant=tenant, category=category, key="vram", data_type="int")
        _login(client, tenant, admin)

        response = client.patch(
            f"/api/v1/categories/{category.id}/fields/{field.id}/",
            data=json.dumps({"data_type": "text"}),
            content_type="application/json",
        )

        assert response.status_code == 200, response.content
        assert response.json()["data_type"] == "text"

    def test_other_attributes_stay_editable_even_when_in_use(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = CategoryFactory(tenant=tenant, name="Compute")
        field = CustomFieldDefFactory(tenant=tenant, category=category, key="vram", data_type="int")
        _login(client, tenant, admin)
        self._create_asset_with_field_value(client, category, field)

        response = client.patch(
            f"/api/v1/categories/{category.id}/fields/{field.id}/",
            data=json.dumps({"label": "VRAM (GB)", "unit": "GB", "order": 5}),
            content_type="application/json",
        )

        assert response.status_code == 200, response.content
        assert response.json()["label"] == "VRAM (GB)"

    def test_deleting_a_field_in_use_cascades_its_values(self, client):
        from apps.assets.models import AssetFieldValue
        from apps.tenancy.context import tenant_context

        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = CategoryFactory(tenant=tenant, name="Compute")
        field = CustomFieldDefFactory(tenant=tenant, category=category, key="vram", data_type="int")
        _login(client, tenant, admin)
        asset_id = self._create_asset_with_field_value(client, category, field)

        response = client.delete(f"/api/v1/categories/{category.id}/fields/{field.id}/")
        assert response.status_code == 204

        with tenant_context(tenant.id):
            assert not AssetFieldValue.objects.filter(field_def_id=field.id).exists()

        detail_resp = client.get(f"/api/v1/assets/{asset_id}/")
        assert detail_resp.status_code == 200
        assert detail_resp.json()["field_values"] == {}
