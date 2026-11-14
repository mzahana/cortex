"""Search and filters on a project's Assets tab (`GET /projects/{id}/assets`).

Regression: the project-hub actions resolve their project via `get_object()`,
which runs DRF's `filter_queryset()` — i.e. `ProjectViewSet`'s OWN
`?search=` (project name/code) and `?status=` (project status) filters. The
asset query string meant for the nested list was being applied to the
PROJECT lookup, so searching assets inside a project 404'd unless the term
happened to appear in the project's name, and the asset Status filter was
validated against project statuses. The main `/assets` list was unaffected,
which is why the same search "worked there and not here".
"""

from __future__ import annotations

import pytest

from apps.assets.models import Asset
from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
    AssetFactory,
    CategoryFactory,
    ProjectFactory,
    TenantFactory,
    UserFactory,
    upgrade_tenant_wide_role,
)
from apps.rbac.permission_keys import ROLE_ADMIN
from apps.tenancy.context import tenant_context

pytestmark = pytest.mark.django_db


@pytest.fixture
def setup(client):
    tenant = TenantFactory()
    with tenant_context(tenant.id):
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant, name="Smart Shuttle", code="SS")
        other = ProjectFactory(tenant=tenant, name="Other Project")
        category = CategoryFactory(tenant=tenant)
        gpu = AssetFactory(
            tenant=tenant,
            category=category,
            project=project,
            name="RTX 4090 GPU",
            status=Asset.Status.IN_USE,
        )
        lidar = AssetFactory(
            tenant=tenant,
            category=category,
            project=project,
            name="Ouster Lidar",
            status=Asset.Status.AVAILABLE,
        )
        # Same name in another project: must never leak into this tab.
        AssetFactory(tenant=tenant, category=category, project=other, name="RTX 4090 GPU")
    response = client.post(
        "/api/v1/auth/login",
        {"tenant": tenant.slug, "email": admin.email, "password": DEFAULT_TEST_PASSWORD},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return project, gpu, lidar


def _ids(response):
    assert response.status_code == 200, response.content
    return {row["id"] for row in response.json()["results"]}


def test_search_finds_assets_by_a_term_not_in_the_project_name(client, setup):
    project, gpu, _lidar = setup
    assert _ids(client.get(f"/api/v1/projects/{project.id}/assets/?search=RTX")) == {gpu.id}


def test_search_is_scoped_to_the_project(client, setup):
    project, gpu, lidar = setup
    assert _ids(client.get(f"/api/v1/projects/{project.id}/assets/?search=lidar")) == {lidar.id}


def test_asset_status_filter_is_not_validated_against_project_status(client, setup):
    project, gpu, _lidar = setup
    assert _ids(client.get(f"/api/v1/projects/{project.id}/assets/?status=in_use")) == {gpu.id}


def test_search_plus_ordering_and_paging(client, setup):
    project, gpu, _lidar = setup
    response = client.get(
        f"/api/v1/projects/{project.id}/assets/?search=gpu&ordering=name&page_size=50"
    )
    assert _ids(response) == {gpu.id}


def test_matches_main_assets_list_restricted_to_project(client, setup):
    """Same term → the project tab returns exactly the main list's hits that
    belong to this project."""
    project, _gpu, _lidar = setup
    main = client.get("/api/v1/assets/?search=RTX")
    assert main.status_code == 200, main.content
    expected = {row["id"] for row in main.json()["results"] if row["project"] == project.id}
    assert _ids(client.get(f"/api/v1/projects/{project.id}/assets/?search=RTX")) == expected


def test_project_expenses_category_filter_does_not_hit_project_lookup(client, setup):
    """Same trap on the sibling nested list: `?search=` on the expenses list
    must not be applied to the project lookup either."""
    project, _gpu, _lidar = setup
    response = client.get(f"/api/v1/projects/{project.id}/expenses/?search=anything")
    assert response.status_code == 200, response.content
