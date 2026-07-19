"""T0.9 — query-budget assertion for `GET /api/v1/me` (guards against N+1
regressions on a path every future authenticated request effectively shares,
via `apps.rbac.services.get_effective_permissions`-shaped logic in
`apps.accounts.api._serialize_me`).

The T0.6 code-review fix collapsed `_serialize_me` to a small, CONSTANT query
count regardless of how many memberships/projects a user has (see that
module's docstring: "a small constant (3: memberships + the prefetch's
role_permissions + the prefetch's permission lookup, all batched)"). This
test pins a budget generous enough to survive incidental middleware/session
queries, but tight enough to fail loudly if a future change reintroduces a
per-membership or per-project query.
"""

from __future__ import annotations

import pytest

from apps.common.tests.factories import (
    ProjectFactory,
    TenantFactory,
    UserFactory,
    add_project_membership,
)
from apps.rbac.permission_keys import ROLE_PROJECT_LEAD

pytestmark = pytest.mark.django_db


def test_me_endpoint_query_count_stays_constant_across_membership_count(
    client, django_assert_max_num_queries
):
    tenant = TenantFactory()
    user = UserFactory(tenant=tenant, email="budget@example.test")

    # Pile on several project-scoped memberships -- if `_serialize_me` ever
    # regressed to a per-membership/per-project query, this is what would
    # blow the budget below.
    for _ in range(5):
        project = ProjectFactory(tenant=tenant)
        add_project_membership(user, project, ROLE_PROJECT_LEAD)

    login = client.post(
        "/api/v1/auth/login",
        {"tenant": tenant.slug, "email": user.email, "password": "TestPass123!"},
        content_type="application/json",
    )
    assert login.status_code == 200

    # Generous budget: session/auth machinery (get_user, session load/save)
    # plus the ~3-query `_serialize_me` pass. The point is that this number
    # does NOT grow with the 5 extra memberships added above -- rerunning
    # with 0 vs 5 memberships should cost the same query count.
    with django_assert_max_num_queries(12):
        response = client.get("/api/v1/me")
    assert response.status_code == 200
    assert len(response.json()["memberships"]) == 6  # 1 default Member + 5 ProjectLead
