"""T1.4 — Asset list endpoint: pagination (bounded page + cursor option),
`?search=` (FTS + pg_trgm fuzzy), `?ordering=` (whitelisted), facet filters
(`category`, `status`, `location`, `project`, `tag`, `is_consumable`),
scope-aware listing (docs/rbac.md §1), and a bounded/N+1-free query budget.

See `docs/tasks/M1-asset-registry.md` T1.4 and `docs/api-and-ui.md`'s Asset
list contract.
"""

from __future__ import annotations

import json

import pytest
from django.db import connection

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
from apps.rbac.permission_keys import ROLE_ADMIN, ROLE_PROJECT_LEAD, ROLE_VIEWER
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
    payload = {"name": "Widget"}
    payload.update(kwargs)
    response = client.post(
        "/api/v1/assets/", data=json.dumps(payload), content_type="application/json"
    )
    assert response.status_code == 201, response.content
    return response.json()


def _names(response) -> list[str]:
    return [a["name"] for a in response.json()["results"]]


class TestAssetListFacetFilters:
    """`?category=`/`?status=`/`?location=`/`?project=`/`?tag=`/
    `?is_consumable=` — every documented facet, individually and combined."""

    def _setup(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        compute = CategoryFactory(tenant=tenant, name="Compute", default_is_consumable=False)
        components = CategoryFactory(tenant=tenant, name="Components", default_is_consumable=True)
        loc_a = LocationFactory(tenant=tenant, name="Lab A")
        loc_b = LocationFactory(tenant=tenant, name="Lab B")
        project = ProjectFactory(tenant=tenant, name="Alpha")
        _login(client, tenant, admin)
        return tenant, compute, components, loc_a, loc_b, project

    def test_filter_by_category(self, client):
        _tenant, compute, components, loc_a, _loc_b, _project = self._setup(client)
        _create_asset(client, name="GPU Box", category=compute.id, location=loc_a.id)
        _create_asset(client, name="Screws", category=components.id, location=loc_a.id)

        resp = client.get(f"/api/v1/assets/?category={compute.id}")
        assert resp.status_code == 200
        assert _names(resp) == ["GPU Box"]

    def test_filter_by_status(self, client):
        _tenant, compute, _components, _loc_a, _loc_b, _project = self._setup(client)
        asset = _create_asset(client, name="Old Drone", category=compute.id)
        client.post(f"/api/v1/assets/{asset['id']}/retire/")

        resp = client.get("/api/v1/assets/?status=retired")
        assert resp.status_code == 200
        assert _names(resp) == ["Old Drone"]

    def test_filter_by_location(self, client):
        _tenant, compute, _components, loc_a, loc_b, _project = self._setup(client)
        _create_asset(client, name="In Lab A", category=compute.id, location=loc_a.id)
        _create_asset(client, name="In Lab B", category=compute.id, location=loc_b.id)

        resp = client.get(f"/api/v1/assets/?location={loc_a.id}")
        assert _names(resp) == ["In Lab A"]

    def test_filter_by_project(self, client):
        _tenant, compute, _components, _loc_a, _loc_b, project = self._setup(client)
        _create_asset(client, name="Project Asset", category=compute.id, project=project.id)
        _create_asset(client, name="Pool Asset", category=compute.id)

        resp = client.get(f"/api/v1/assets/?project={project.id}")
        assert _names(resp) == ["Project Asset"]

    def test_filter_by_tag(self, client):
        _tenant, compute, _components, _loc_a, _loc_b, _project = self._setup(client)
        _create_asset(client, name="Tagged", category=compute.id, tags=["gpu", "hot"])
        _create_asset(client, name="Untagged", category=compute.id)

        with tenant_context(_tenant.id):
            from apps.catalog.models import Tag

            gpu_tag = Tag.objects.get(name="gpu")

        resp = client.get(f"/api/v1/assets/?tag={gpu_tag.id}")
        assert _names(resp) == ["Tagged"]

    def test_filter_by_is_consumable(self, client):
        _tenant, compute, components, _loc_a, _loc_b, _project = self._setup(client)
        _create_asset(client, name="GPU Box", category=compute.id)
        _create_asset(client, name="Screws", category=components.id)

        resp = client.get("/api/v1/assets/?is_consumable=true")
        assert _names(resp) == ["Screws"]
        resp = client.get("/api/v1/assets/?is_consumable=false")
        assert _names(resp) == ["GPU Box"]

    def test_combined_filters(self, client):
        _tenant, compute, components, loc_a, loc_b, project = self._setup(client)
        _create_asset(
            client, name="Match", category=compute.id, location=loc_a.id, project=project.id
        )
        _create_asset(client, name="WrongLocation", category=compute.id, location=loc_b.id)
        _create_asset(client, name="WrongCategory", category=components.id, location=loc_a.id)

        resp = client.get(
            f"/api/v1/assets/?category={compute.id}&location={loc_a.id}&project={project.id}"
        )
        assert _names(resp) == ["Match"]

    def test_filter_value_from_another_tenant_matches_nothing(self, client):
        """R4: a category/location/project/tag id from ANOTHER tenant must
        never match/leak — it simply matches zero rows (no error, no
        cross-tenant existence oracle)."""
        tenant_a, compute_a, _components_a, _loc_a, _loc_b, _project_a = self._setup(client)
        other_tenant = TenantFactory()
        other_category = CategoryFactory(tenant=other_tenant, name="OtherTenantCategory")

        _create_asset(client, name="Tenant A Asset", category=compute_a.id)

        resp = client.get(f"/api/v1/assets/?category={other_category.id}")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0


class TestAssetListOrdering:
    def test_ordering_by_whitelisted_field(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = CategoryFactory(tenant=tenant)
        _login(client, tenant, admin)
        _create_asset(client, name="Bravo", category=category.id)
        _create_asset(client, name="Alpha", category=category.id)

        resp = client.get("/api/v1/assets/?ordering=name")
        assert _names(resp) == ["Alpha", "Bravo"]

        resp = client.get("/api/v1/assets/?ordering=-name")
        assert _names(resp) == ["Bravo", "Alpha"]

    def test_ordering_rejects_non_whitelisted_field(self, client):
        """An arbitrary/non-whitelisted `?ordering=` value must not inject a
        column into the query (SQL-injection-adjacent hardening) — DRF's
        `OrderingFilter` silently drops any field not in `ordering_fields`
        and falls back to the view's default `-created_at`, rather than
        erroring OR blindly ordering by whatever the client asked for."""
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = CategoryFactory(tenant=tenant)
        _login(client, tenant, admin)
        _create_asset(client, name="First", category=category.id)
        _create_asset(client, name="Second", category=category.id)

        resp = client.get("/api/v1/assets/?ordering=qr_token")
        assert resp.status_code == 200
        # Falls back to the default `-created_at` (Second created after
        # First) rather than honoring the non-whitelisted field.
        assert _names(resp) == ["Second", "First"]


class TestAssetListSearch:
    """`?search=` — FTS (`websearch_to_tsquery('english', ...)` over
    `search_vector`) UNIONed with `pg_trgm` fuzzy matching on `name`/
    `serial_number`."""

    def _setup(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = CategoryFactory(tenant=tenant, name="Compute")
        _login(client, tenant, admin)
        return tenant, category

    def test_fts_matches_name(self, client):
        _tenant, category = self._setup(client)
        _create_asset(client, name="RTX 4090 GPU Server", category=category.id)
        _create_asset(client, name="Soldering Iron", category=category.id)

        resp = client.get("/api/v1/assets/?search=GPU")
        assert resp.status_code == 200
        assert _names(resp) == ["RTX 4090 GPU Server"]

    def test_fts_matches_serial_number(self, client):
        _tenant, category = self._setup(client)
        _create_asset(client, name="Widget", category=category.id, serial_number="SN-ROBOT-42")
        _create_asset(client, name="Other Widget", category=category.id, serial_number="SN-000")

        resp = client.get("/api/v1/assets/?search=ROBOT")
        assert _names(resp) == ["Widget"]

    def test_fuzzy_typo_still_matches(self, client):
        """A misspelled query ("solderng" for "soldering", a dropped-letter
        typo — `similarity('Soldering Iron', 'solderng') ≈ 0.41`, comfortably
        over pg_trgm's default 0.3 threshold) must still match via the
        pg_trgm similarity fallback even though FTS (exact-stem matching)
        would not stem "solderng" to "soldering"."""
        _tenant, category = self._setup(client)
        _create_asset(client, name="Soldering Iron", category=category.id)
        _create_asset(client, name="Unrelated Widget", category=category.id)

        resp = client.get("/api/v1/assets/?search=solderng")
        assert resp.status_code == 200
        assert _names(resp) == ["Soldering Iron"]

    def test_search_with_no_matches_returns_empty(self, client):
        _tenant, category = self._setup(client)
        _create_asset(client, name="Widget", category=category.id)

        resp = client.get("/api/v1/assets/?search=zzzznomatchzzzz")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    def _make_category_with_notes_field(self, tenant):
        """A category with one optional TEXT custom field ("notes") — used
        to construct a match that ONLY hits `search_vector` via the
        custom-field-value weight (C), never the name (weight A), so its
        `SearchRank` is provably lower than a name match."""
        from apps.catalog.models import CustomFieldDef

        category = CategoryFactory(tenant=tenant, name="Sensors")
        CustomFieldDef.all_objects.create(
            tenant=tenant,
            category=category,
            key="notes",
            label="Notes",
            data_type=CustomFieldDef.DataType.TEXT,
            required=False,
            order=0,
        )
        return category

    def test_relevance_ranking_orders_higher_relevance_first_regardless_of_creation_date(
        self, client
    ):
        """A REAL (non-vacuous) multi-row relevance-ranking proof — the
        opposite of the code-review-flagged single-match test this replaces.

        Three rows, all matching `?search=lidar`, deliberately created in an
        order where relevance rank and creation date DISAGREE, so a
        `-created_at`-only ordering (the code-review-confirmed bug: DRF's
        `OrderingFilter` was silently re-applying the view's default
        `-created_at` and clobbering `AssetSearchFilter`'s own
        `.order_by("-rank", ...)`) would produce the OPPOSITE order of the
        correct, relevance-ranked one — making this test fail loudly if the
        bug ever regresses, rather than passing vacuously.

        Creation order (oldest -> newest): name-match, custom-field-match,
        fuzzy-only-match.
        - `name_match` (weight A, name contains "Lidar" literally) — HIGHEST
          rank, but OLDEST (would sort LAST under plain `-created_at`).
        - `custom_field_match` (weight C, only the "notes" custom field
          value contains "lidar" — the name doesn't) — MIDDLE rank.
        - `fuzzy_match` (no FTS lexeme match at all — "Lidaar" doesn't stem
          to "lidar" — matched ONLY via the `pg_trgm` `name__trigram_similar`
          fallback; `SearchRank` against a non-matching `search_vector` is 0)
          — LOWEST rank, but NEWEST (would sort FIRST under plain
          `-created_at`). `similarity('Lidaar Unit', 'lidar') ≈ 0.385`
          (verified directly against this Postgres/pg_trgm build), over the
          0.3 default threshold.

        Correct (rank-ordered) result: [name_match, custom_field_match,
        fuzzy_match] — the exact REVERSE of the buggy (date-ordered) result
        [fuzzy_match, custom_field_match, name_match].
        """
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = self._make_category_with_notes_field(tenant)
        _login(client, tenant, admin)

        name_match = _create_asset(client, name="Lidar Scanner Alpha", category=category.id)
        custom_field_match = _create_asset(
            client,
            name="Storage Unit",
            category=category.id,
            custom_field_values={"notes": "Includes a lidar reference manual"},
        )
        # "Lidaar" (extra letter) — `similarity('Lidaar Unit', 'lidar') ≈
        # 0.385`, over the 0.3 threshold — but does NOT contain "lidar" as a
        # literal substring/lexeme (extra "a"), so it is NOT an FTS match at
        # all (rank 0 via SearchRank against a non-matching search_vector),
        # only a trigram-similarity match.
        fuzzy_match = _create_asset(client, name="Lidaar Unit", category=category.id)

        resp = client.get("/api/v1/assets/?search=lidar")
        assert resp.status_code == 200
        names = _names(resp)
        assert names == ["Lidar Scanner Alpha", "Storage Unit", "Lidaar Unit"]
        # Sanity: this is the REVERSE of pure `-created_at` (i.e. reverse
        # insertion order) — proving rank, not date, drove this order.
        assert names == list(
            reversed([fuzzy_match["name"], custom_field_match["name"], name_match["name"]])
        )

    def test_explicit_ordering_overrides_relevance_ranking_during_search(self, client):
        """An explicit `?ordering=` must still win over relevance ranking —
        `AssetSearchFilter` deliberately skips its own rank-ordering when an
        `?ordering=` param is present, and (post-fix) `OrderingFilter` is the
        one applying it, unclobbered."""
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = self._make_category_with_notes_field(tenant)
        _login(client, tenant, admin)

        _create_asset(client, name="Lidar Scanner Alpha", category=category.id)
        _create_asset(
            client,
            name="Storage Unit",
            category=category.id,
            custom_field_values={"notes": "Includes a lidar reference manual"},
        )

        # By rank, "Lidar Scanner Alpha" (name match) outranks "Storage Unit"
        # (custom-field-only match) — see the test above. `?ordering=name`
        # must override that and sort alphabetically instead.
        resp = client.get("/api/v1/assets/?search=lidar&ordering=name")
        assert resp.status_code == 200
        assert _names(resp) == ["Lidar Scanner Alpha", "Storage Unit"]

        resp = client.get("/api/v1/assets/?search=lidar&ordering=-name")
        assert _names(resp) == ["Storage Unit", "Lidar Scanner Alpha"]

    def test_explain_uses_gin_index_for_fts(self, client):
        """Correctness proof for the T1.4 exit criterion: force the planner
        to prefer index scans (`enable_seqscan=off`) — at this tiny test row
        count Postgres would otherwise legitimately choose a sequential scan
        regardless of the index — and confirm the FTS predicate's plan
        references the `assets_asset_search_vector_gin` index, proving the
        query is INDEX-ELIGIBLE (uses `websearch_to_tsquery('english', ...)`
        against `search_vector`, the exact shape/config that index was built
        for)."""
        tenant, category = self._setup(client)
        _create_asset(client, name="RTX 4090 GPU Server", category=category.id)

        with tenant_context(tenant.id):
            with connection.cursor() as cur:
                cur.execute("SET LOCAL enable_seqscan = off")
                cur.execute(
                    "EXPLAIN SELECT id FROM assets_asset "
                    "WHERE search_vector @@ websearch_to_tsquery('english', %s)",
                    ["GPU"],
                )
                plan = "\n".join(row[0] for row in cur.fetchall())
        assert "assets_asset_search_vector_gin" in plan

    def test_explain_uses_trgm_index_for_fuzzy_name_match(self, client):
        tenant, category = self._setup(client)
        _create_asset(client, name="Racing Drone Mk2", category=category.id)

        with tenant_context(tenant.id):
            with connection.cursor() as cur:
                cur.execute("SET LOCAL enable_seqscan = off")
                cur.execute("EXPLAIN SELECT id FROM assets_asset WHERE name %% %s", ["dorne"])
                plan = "\n".join(row[0] for row in cur.fetchall())
        assert "assets_asset_name_trgm" in plan


class TestAssetListScopeAware:
    """docs/rbac.md §1 scope rule, carried into the LIST endpoint (T1.4):
    tenant-wide `asset.view` -> sees every tenant asset; ONLY project-scoped
    `asset.view` -> sees ONLY that project's assets (never another project,
    never the general pool)."""

    def test_tenant_wide_viewer_sees_every_asset(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant)
        category = CategoryFactory(tenant=tenant)
        _login(client, tenant, admin)
        _create_asset(client, name="Pool Asset", category=category.id)
        _create_asset(client, name="Project Asset", category=category.id, project=project.id)
        client.post("/api/v1/auth/logout")

        viewer = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(viewer, ROLE_VIEWER)  # tenant-wide asset.view
        _login(client, tenant, viewer)

        resp = client.get("/api/v1/assets/")
        assert sorted(_names(resp)) == ["Pool Asset", "Project Asset"]

    def test_pure_project_scoped_user_sees_only_own_project_never_pool_or_other_project(
        self, client
    ):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        own_project = ProjectFactory(tenant=tenant, name="Own")
        other_project = ProjectFactory(tenant=tenant, name="Other")
        category = CategoryFactory(tenant=tenant)
        _login(client, tenant, admin)
        _create_asset(
            client, name="Own Project Asset", category=category.id, project=own_project.id
        )
        _create_asset(
            client, name="Other Project Asset", category=category.id, project=other_project.id
        )
        _create_asset(client, name="General Pool Asset", category=category.id)
        client.post("/api/v1/auth/logout")

        lead = UserFactory(tenant=tenant)
        # Remove the auto-assigned tenant-wide Member membership (which would
        # otherwise ALSO grant tenant-wide asset.view) so this user's ONLY
        # membership is the project-scoped ProjectLead grant below — the
        # "pure ProjectLead" scenario this test exists to prove.
        Membership.all_objects.filter(user=lead, project__isnull=True).delete()
        add_project_membership(lead, own_project, ROLE_PROJECT_LEAD)
        _login(client, tenant, lead)

        resp = client.get("/api/v1/assets/")
        assert resp.status_code == 200
        assert _names(resp) == ["Own Project Asset"]

    def test_pure_project_scoped_user_is_not_denied_the_list_entirely(self, client):
        """The M0 "pure-ProjectLead over-deny" bug CLAUDE.md forbids
        reintroducing: a user with ONLY a project-scoped grant must still get
        a 200 (possibly an empty/partial list), never a blanket 403."""
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        project = ProjectFactory(tenant=tenant)
        _login(client, tenant, admin)
        client.post("/api/v1/auth/logout")

        lead = UserFactory(tenant=tenant)
        Membership.all_objects.filter(user=lead, project__isnull=True).delete()
        add_project_membership(lead, project, ROLE_PROJECT_LEAD)
        _login(client, tenant, lead)

        resp = client.get("/api/v1/assets/")
        assert resp.status_code == 200


class TestAssetListPagination:
    def test_page_number_pagination_bounded_max_page_size(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = CategoryFactory(tenant=tenant)
        _login(client, tenant, admin)
        for i in range(5):
            _create_asset(client, name=f"Widget {i}", category=category.id)

        # A client asking for an absurdly large page_size is capped, never an
        # unbounded "load-all" (CLAUDE.md: "frontend never loads 'all
        # assets'"); `max_page_size=100` in `BoundedPageNumberPagination`.
        resp = client.get("/api/v1/assets/?page_size=100000")
        assert resp.status_code == 200
        # Only 5 rows exist, so this alone doesn't prove the cap — the real
        # proof is `BoundedPageNumberPagination.max_page_size` unit-testable
        # behavior below.
        assert resp.json()["count"] == 5

    def test_cursor_pagination_option(self, client):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = CategoryFactory(tenant=tenant)
        _login(client, tenant, admin)
        for i in range(5):
            _create_asset(client, name=f"Widget {i}", category=category.id)

        # `?cursor=` (empty, i.e. "start of the list") opts into cursor mode
        # per-request — see `AssetViewSet.paginator`.
        resp = client.get("/api/v1/assets/?cursor=&page_size=2")
        assert resp.status_code == 200
        body = resp.json()
        assert "results" in body
        assert "next" in body and "previous" in body
        assert "count" not in body  # cursor pagination has no total count
        assert len(body["results"]) == 2

        next_url = body["next"]
        assert next_url
        # Follow the `next` link (mirrors a real client) — relative path
        # only, `client.get` doesn't need the host part.
        from urllib.parse import urlparse

        next_path = urlparse(next_url).path + "?" + urlparse(next_url).query
        resp2 = client.get(next_path)
        assert resp2.status_code == 200
        assert len(resp2.json()["results"]) == 2
        # No overlap between the two pages.
        assert {a["id"] for a in body["results"]}.isdisjoint(
            {a["id"] for a in resp2.json()["results"]}
        )


class TestAssetListQueryBudget:
    """The query count must be BOUNDED and INDEPENDENT of page_size — proof
    there's no N+1 hiding in the serializer's prefetches."""

    def test_query_count_independent_of_page_size(self, client, django_assert_max_num_queries):
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        category = CategoryFactory(tenant=tenant, name="Compute")
        location = LocationFactory(tenant=tenant)
        project = ProjectFactory(tenant=tenant)
        _login(client, tenant, admin)
        for i in range(50):
            _create_asset(
                client,
                name=f"Widget {i}",
                category=category.id,
                location=location.id,
                project=project.id,
                tags=["gpu", "lab"],
            )

        with django_assert_max_num_queries(20) as ctx_1:
            resp_1 = client.get("/api/v1/assets/?page_size=1")
        assert resp_1.status_code == 200
        assert len(resp_1.json()["results"]) == 1
        queries_for_1 = len(ctx_1.captured_queries)

        with django_assert_max_num_queries(20) as ctx_50:
            resp_50 = client.get("/api/v1/assets/?page_size=50")
        assert resp_50.status_code == 200
        assert len(resp_50.json()["results"]) == 50
        queries_for_50 = len(ctx_50.captured_queries)

        # The exact bounded constant — same number of queries regardless of
        # how many rows are on the page.
        assert queries_for_1 == queries_for_50
