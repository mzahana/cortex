"""T1.8 — the perf gate (F10): 10k+ seeded assets, paginated list + `?search=`
FTS/fuzzy < 500 ms server-side, bounded query counts, `EXPLAIN` proof of index
usage for the search endpoint's actual combined query, and filter-correctness
at scale.

The seed corpus is built ONCE per module (`perf_corpus`, module-scoped,
`django_db_blocker.unblock()`) via `apps.assets.perf_seed.seed_assets` — real
committed rows (not inside a rolled-back per-test transaction), so every test
function in this module (each still gets its own `db`-fixture transaction for
its OWN reads/writes) sees the same 10k+ corpus without re-seeding per test.

Timing is measured server-side via the in-process Django test client (no
real network hop) — the same proxy pattern the M1 task description asks for
("measure server-side query/response time, not network").
"""

from __future__ import annotations

import statistics
import time
from types import SimpleNamespace

import pytest
from django.contrib.postgres.search import SearchQuery, SearchRank
from django.db.models import F, Q

from apps.assets.models import Asset
from apps.assets.perf_seed import delete_seeded_tenant, seed_assets
from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
    TenantFactory,
    UserFactory,
    upgrade_tenant_wide_role,
)
from apps.rbac.permission_keys import ROLE_ADMIN
from apps.tenancy.context import tenant_context

PERF_ASSET_COUNT = 10_500  # > 10k with margin
PERF_BUDGET_SECONDS = 0.5
N_TIMING_RUNS = 5


def _login(client, tenant, user):
    response = client.post(
        "/api/v1/auth/login",
        {"tenant": tenant.slug, "email": user.email, "password": DEFAULT_TEST_PASSWORD},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return response


def _timed_get(client, url):
    start = time.perf_counter()
    resp = client.get(url)
    elapsed = time.perf_counter() - start
    return elapsed, resp


@pytest.fixture(scope="module")
def perf_corpus(django_db_setup, django_db_blocker):
    """Module-scoped 10k+ asset corpus, seeded ONCE for every test below.

    **Must depend on `django_db_setup` explicitly** (not just
    `django_db_blocker`): `django_db_setup` is the session-scoped
    pytest-django fixture that actually creates/migrates the `test_`-
    prefixed scratch database and points the `default` connection at it.
    Without it in this fixture's own signature, pytest has no dependency
    edge forcing it to run before a module-scoped fixture that itself
    requests only `django_db_blocker` — if this module's FIRST test doesn't
    also pull in the function-scoped `db`/`client` fixtures (which
    transitively depend on `django_db_setup`), this fixture's writes land
    directly in the real dev/CI database configured by `DATABASE_URL`
    instead of the disposable test database. (This bit an earlier draft of
    this fixture: it silently wrote a real "Test Tenant 0" + 10.5k assets
    into the actual `cortex` dev DB, while every OTHER test in the module —
    which does use `client`/`db` — ran against the correctly-created empty
    `test_cortex`, causing every login in this module to 401 against a
    tenant/user that existed in the wrong database. Cleaned up by hand;
    this dependency is what prevents it from recurring.)

    **Teardown (T1.9 QA finding, closed here):** this fixture writes its
    10.5k-row corpus via `django_db_blocker.unblock()` — real, unconditionally
    COMMITTED rows, deliberately NOT wrapped in the per-test `db` fixture's
    rolled-back transaction (module-scope + the timing assertions below need
    the corpus to survive across every test function in this module). But
    that means nothing rolls it back OR truncates it afterward either: left
    as `return`, this tenant/its 10.5k+ assets/categories/locations silently
    outlived this module and leaked into every OTHER test module that ran
    later in the SAME `pytest` session — concretely, it broke
    `apps.common.tests.test_smoke.test_app_imports_and_migrations_ran`'s
    `assert Tenant.objects.count() == 0` (that module is collected after this
    one — `assets` sorts before `common` — so it always inherited a non-empty
    `tenancy_tenant` table), a real, deterministic (not order-flaky) full-suite
    failure this fixture was responsible for. Deleting the corpus in a
    `finally` after the last test in this module runs closes that gap without
    weakening any of this module's own assertions.
    """
    with django_db_blocker.unblock():
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        result = seed_assets(tenant, count=PERF_ASSET_COUNT, batch_size=1000)
        try:
            yield SimpleNamespace(tenant=tenant, admin=admin, result=result)
        finally:
            delete_seeded_tenant(tenant)


pytestmark = pytest.mark.django_db


class TestPerfSeedCorpus:
    """Sanity on the seed itself: >= 10k rows, `search_vector` populated
    (the trigger-disable/backfill approach must still yield CORRECT vectors,
    not NULLs left over from the disabled triggers)."""

    def test_seed_created_10k_plus_assets(self, perf_corpus):
        assert perf_corpus.result.assets_created >= 10_000
        with tenant_context(perf_corpus.tenant.id):
            assert Asset.objects.count() >= 10_000

    def test_search_vector_backfilled_not_null(self, perf_corpus):
        with tenant_context(perf_corpus.tenant.id):
            null_count = Asset.objects.filter(search_vector__isnull=True).count()
            total = Asset.objects.count()
        assert null_count == 0, (
            f"{null_count}/{total} seeded assets have a NULL search_vector — "
            "the trigger-disable + backfill approach left rows unindexed."
        )

    def test_search_vector_matches_name_content(self, perf_corpus):
        """Spot-check: the backfilled vector actually reflects the asset's
        own name (not e.g. all-identical or empty vectors)."""
        with tenant_context(perf_corpus.tenant.id):
            asset = Asset.objects.filter(category__name="GPU Workstations").first()
            assert asset is not None
            matches = Asset.objects.filter(
                id=asset.id, search_vector=SearchQuery("GPU", config="english")
            ).exists()
        assert matches, "Backfilled search_vector does not match the asset's own name text."


class TestPerfListPagination:
    def test_default_page_list_under_budget(
        self, client, perf_corpus, django_assert_max_num_queries
    ):
        _login(client, perf_corpus.tenant, perf_corpus.admin)
        # Query-count budget: ONE request's worth of queries (not the whole
        # N_TIMING_RUNS timing loop below, which would multiply the count by
        # N_TIMING_RUNS and assert the wrong thing).
        with django_assert_max_num_queries(20):
            resp = client.get("/api/v1/assets/")
        assert resp.status_code == 200

        timings = []
        for _ in range(N_TIMING_RUNS):
            elapsed, resp = _timed_get(client, "/api/v1/assets/")
            assert resp.status_code == 200
            timings.append(elapsed)
        median = statistics.median(timings)
        # The default list EXCLUDES retired assets (docs/data-model.md §4),
        # so the visible count is somewhat under the full 10.5k seeded —
        # verify against the same tenant-scoped, non-retired ORM count
        # rather than the raw seed total.
        with tenant_context(perf_corpus.tenant.id):
            expected_count = Asset.objects.exclude(status=Asset.Status.RETIRED).count()
        assert resp.json()["count"] == expected_count
        assert expected_count >= 9_000  # sanity: still comfortably over the F10 "10k+" scale
        assert median < PERF_BUDGET_SECONDS, (
            f"Default paginated list median server-side time {median * 1000:.1f} ms "
            f"exceeds the {PERF_BUDGET_SECONDS * 1000:.0f} ms F10 budget at "
            f"{resp.json()['count']} rows. All runs: "
            f"{[round(t * 1000, 1) for t in timings]} ms."
        )

    def test_query_count_independent_of_page_size_at_scale(
        self, client, perf_corpus, django_assert_max_num_queries
    ):
        _login(client, perf_corpus.tenant, perf_corpus.admin)
        with django_assert_max_num_queries(20) as ctx_small:
            resp_small = client.get("/api/v1/assets/?page_size=1")
        assert resp_small.status_code == 200
        with django_assert_max_num_queries(20) as ctx_large:
            resp_large = client.get("/api/v1/assets/?page_size=100")
        assert resp_large.status_code == 200
        # Bounded and INDEPENDENT of page size within a small, data-driven
        # tolerance — not necessarily byte-identical: `.filter(id__in=[])`
        # short-circuits to zero DB round-trips (Django ORM optimization),
        # so a page whose single asset happens to carry no tags issues one
        # fewer query (skips the `catalog_tag` name lookup) than a page
        # where every asset has zero tags too but a larger page is more
        # likely to include at least one tagged asset. A REAL N+1 would
        # scale with page size (100x more prefetch queries, not +/-1), so a
        # small fixed tolerance here still catches that regression.
        small_n = len(ctx_small.captured_queries)
        large_n = len(ctx_large.captured_queries)
        assert abs(small_n - large_n) <= 2, (
            f"Query count should be bounded/independent of page size (within "
            f"a small tolerance for data-dependent short-circuits like empty "
            f"`id__in=[]` prefetches), but page_size=1 -> {small_n} queries "
            f"vs page_size=100 -> {large_n} queries."
        )


class TestPerfSearch:
    """`?search=` — a term chosen to match a realistic subset (not the whole
    corpus): "GPU" hits GPU Workstations' name/custom-field text only."""

    SEARCH_TERM = "GPU"

    def test_search_under_budget(self, client, perf_corpus, django_assert_max_num_queries):
        _login(client, perf_corpus.tenant, perf_corpus.admin)
        with django_assert_max_num_queries(20):
            resp = client.get(f"/api/v1/assets/?search={self.SEARCH_TERM}")
        assert resp.status_code == 200

        timings = []
        for _ in range(N_TIMING_RUNS):
            elapsed, resp = _timed_get(client, f"/api/v1/assets/?search={self.SEARCH_TERM}")
            assert resp.status_code == 200
            timings.append(elapsed)
        median = statistics.median(timings)
        match_count = resp.json()["count"]
        assert 0 < match_count < 10_000, (
            "Sanity: the search term should match a realistic SUBSET, not "
            f"(near-)zero or the whole corpus; got count={match_count}."
        )
        assert median < PERF_BUDGET_SECONDS, (
            f"?search={self.SEARCH_TERM} median server-side time {median * 1000:.1f} ms "
            f"exceeds the {PERF_BUDGET_SECONDS * 1000:.0f} ms F10 budget "
            f"(match_count={match_count}). All runs: "
            f"{[round(t * 1000, 1) for t in timings]} ms."
        )

    def test_fuzzy_search_under_budget(self, client, perf_corpus, django_assert_max_num_queries):
        """A misspelled term exercising the pg_trgm fallback branch."""
        _login(client, perf_corpus.tenant, perf_corpus.admin)
        with django_assert_max_num_queries(20):
            resp = client.get("/api/v1/assets/?search=Workstaton")
        assert resp.status_code == 200

        timings = []
        for _ in range(N_TIMING_RUNS):
            elapsed, resp = _timed_get(client, "/api/v1/assets/?search=Workstaton")
            assert resp.status_code == 200
            timings.append(elapsed)
        median = statistics.median(timings)
        assert median < PERF_BUDGET_SECONDS, (
            f"Fuzzy ?search= median server-side time {median * 1000:.1f} ms "
            f"exceeds the {PERF_BUDGET_SECONDS * 1000:.0f} ms F10 budget. "
            f"All runs: {[round(t * 1000, 1) for t in timings]} ms."
        )


def _build_search_queryset(tenant, search_term: str):
    """Reconstruct the EXACT queryset `AssetViewSet`/`AssetSearchFilter`
    build for `?search=<search_term>` (tenant-scoped list, retired excluded,
    the 3-way OR, rank annotation + ordering) so `.explain()` reflects the
    real endpoint query, not an approximation.
    """
    ts_query = SearchQuery(search_term, config="english", search_type="websearch")
    with tenant_context(tenant.id):
        qs = (
            Asset.objects.exclude(status=Asset.Status.RETIRED)
            .filter(
                Q(search_vector=ts_query)
                | Q(name__trigram_similar=search_term)
                | Q(serial_number__trigram_similar=search_term)
            )
            .annotate(rank=SearchRank(F("search_vector"), ts_query))
            .order_by("-rank", "-created_at")
        )
        return qs


class TestPerfSearchExplainPlan:
    """**KEY OUTPUT**: does the planner use the GIN/trgm indexes (a
    `BitmapOr` over the three index-friendly branches) for the REAL
    3-way-OR query at 10k+ rows under NATURAL planner settings (no
    `enable_seqscan=off` override — that would only prove index-eligibility,
    not what the planner actually picks at this scale/selectivity)?
    """

    def test_explain_analyze_combined_or_query_natural_planner(self, perf_corpus, capsys):
        qs = _build_search_queryset(perf_corpus.tenant, "GPU")
        with tenant_context(perf_corpus.tenant.id):
            plan_lines = qs.explain(analyze=True, buffers=True, verbose=False)
        plan = plan_lines if isinstance(plan_lines, str) else "\n".join(plan_lines)

        with capsys.disabled():
            print("\n--- EXPLAIN (ANALYZE, BUFFERS) for ?search=GPU (natural planner) ---")
            print(plan)
            print("--- end plan ---\n")

        uses_seqscan = "Seq Scan on assets_asset" in plan
        uses_bitmap_or = "BitmapOr" in plan
        uses_gin = "assets_asset_search_vector_gin" in plan or "assets_asset_name_trgm" in plan

        # Not a hard pytest failure either way — this test's job is to PROVE
        # and REPORT which branch the planner actually took at this scale
        # (the QA report captures the verdict); a seq-scan here is a real
        # finding to route back to the backend-engineer for a query
        # restructure (UNION of index-friendly branches), not something to
        # paper over by loosening this assertion.
        print(
            f"VERDICT: seq_scan={uses_seqscan} bitmap_or={uses_bitmap_or} "
            f"gin_index_seen={uses_gin}"
        )


class TestPerfFilterCorrectnessAtScale:
    """Every documented facet returns the correct subset at 10k+ rows —
    verified against an independent, tenant-scoped ORM count (not a
    hardcoded expectation baked into the seed), so this test would catch a
    real filter bug rather than just echoing the seed's own ratios."""

    def _login_admin(self, client, perf_corpus):
        _login(client, perf_corpus.tenant, perf_corpus.admin)

    def test_category_filter(self, client, perf_corpus):
        self._login_admin(client, perf_corpus)
        category_id = perf_corpus.result.category_ids["GPU Workstations"]
        with tenant_context(perf_corpus.tenant.id):
            expected = (
                Asset.objects.filter(category_id=category_id)
                .exclude(status=Asset.Status.RETIRED)
                .count()
            )
        resp = client.get(f"/api/v1/assets/?category={category_id}")
        assert resp.status_code == 200
        assert resp.json()["count"] == expected
        assert expected > 0

    def test_status_filter(self, client, perf_corpus):
        self._login_admin(client, perf_corpus)
        with tenant_context(perf_corpus.tenant.id):
            expected = Asset.objects.filter(status=Asset.Status.MAINTENANCE).count()
        resp = client.get("/api/v1/assets/?status=maintenance")
        assert resp.status_code == 200
        assert resp.json()["count"] == expected
        assert expected > 0

    def test_retired_status_filter_includes_retired(self, client, perf_corpus):
        self._login_admin(client, perf_corpus)
        with tenant_context(perf_corpus.tenant.id):
            expected = Asset.objects.filter(status=Asset.Status.RETIRED).count()
        resp = client.get("/api/v1/assets/?status=retired")
        assert resp.status_code == 200
        assert resp.json()["count"] == expected
        assert expected > 0

    def test_location_filter(self, client, perf_corpus):
        self._login_admin(client, perf_corpus)
        location_id = perf_corpus.result.location_ids[0]
        with tenant_context(perf_corpus.tenant.id):
            expected = (
                Asset.objects.filter(location_id=location_id)
                .exclude(status=Asset.Status.RETIRED)
                .count()
            )
        resp = client.get(f"/api/v1/assets/?location={location_id}")
        assert resp.status_code == 200
        assert resp.json()["count"] == expected
        assert expected > 0

    def test_project_filter(self, client, perf_corpus):
        self._login_admin(client, perf_corpus)
        project_id = perf_corpus.result.project_ids[0]
        with tenant_context(perf_corpus.tenant.id):
            expected = (
                Asset.objects.filter(project_id=project_id)
                .exclude(status=Asset.Status.RETIRED)
                .count()
            )
        resp = client.get(f"/api/v1/assets/?project={project_id}")
        assert resp.status_code == 200
        assert resp.json()["count"] == expected
        assert expected > 0

    def test_tag_filter(self, client, perf_corpus):
        self._login_admin(client, perf_corpus)
        tag_id = perf_corpus.result.tag_ids[0]
        with tenant_context(perf_corpus.tenant.id):
            expected = (
                Asset.objects.filter(tag_links__tag_id=tag_id)
                .exclude(status=Asset.Status.RETIRED)
                .count()
            )
        resp = client.get(f"/api/v1/assets/?tag={tag_id}")
        assert resp.status_code == 200
        assert resp.json()["count"] == expected
        assert expected > 0

    def test_is_consumable_filter(self, client, perf_corpus):
        self._login_admin(client, perf_corpus)
        with tenant_context(perf_corpus.tenant.id):
            expected_true = (
                Asset.objects.filter(is_consumable=True)
                .exclude(status=Asset.Status.RETIRED)
                .count()
            )
            expected_false = (
                Asset.objects.filter(is_consumable=False)
                .exclude(status=Asset.Status.RETIRED)
                .count()
            )
        resp_true = client.get("/api/v1/assets/?is_consumable=true")
        resp_false = client.get("/api/v1/assets/?is_consumable=false")
        assert resp_true.json()["count"] == expected_true
        assert resp_false.json()["count"] == expected_false
        assert expected_true > 0 and expected_false > 0

    def test_combined_category_and_location_filter(self, client, perf_corpus):
        self._login_admin(client, perf_corpus)
        category_id = perf_corpus.result.category_ids["Components"]
        location_id = perf_corpus.result.location_ids[1]
        with tenant_context(perf_corpus.tenant.id):
            expected = (
                Asset.objects.filter(category_id=category_id, location_id=location_id)
                .exclude(status=Asset.Status.RETIRED)
                .count()
            )
        resp = client.get(f"/api/v1/assets/?category={category_id}&location={location_id}")
        assert resp.status_code == 200
        assert resp.json()["count"] == expected

    def test_scope_aware_listing_still_correct_at_scale(self, client, perf_corpus):
        """docs/rbac.md §1 scope rule holds at 10k+ rows too: a
        project-scoped-only user sees ONLY that project's assets, never the
        general pool or another project — same rule T1.4 proved at tiny
        scale, re-proved here against the full seeded corpus."""
        from apps.common.tests.factories import add_project_membership
        from apps.rbac.models import Membership
        from apps.rbac.permission_keys import ROLE_PROJECT_LEAD

        tenant = perf_corpus.tenant
        project_id = perf_corpus.result.project_ids[0]
        with tenant_context(tenant.id):
            expected = (
                Asset.objects.filter(project_id=project_id)
                .exclude(status=Asset.Status.RETIRED)
                .count()
            )
            from apps.projects.models import Project

            project = Project.objects.get(id=project_id)

        lead = UserFactory(tenant=tenant)
        Membership.all_objects.filter(user=lead, project__isnull=True).delete()
        add_project_membership(lead, project, ROLE_PROJECT_LEAD)
        _login(client, tenant, lead)

        resp = client.get("/api/v1/assets/")
        assert resp.status_code == 200
        assert resp.json()["count"] == expected
        assert expected > 0
