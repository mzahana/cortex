"""T5.5 perf gate: `GET /api/v1/dashboard/summary` stays inside the < 800 ms
budget at the 10k+-asset scale from T1.8's `apps.assets.perf_seed`, cached
AND cold. Mirrors `apps.assets.tests.test_perf_10k`'s module-scoped-corpus
pattern (see that module's docstring for why `django_db_setup` must be an
explicit fixture dependency here too).
"""

from __future__ import annotations

import statistics
import time
from types import SimpleNamespace

import pytest

from apps.assets.perf_seed import delete_seeded_tenant, seed_assets
from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
    TenantFactory,
    UserFactory,
    upgrade_tenant_wide_role,
)
from apps.dashboard.cache import invalidate_tenant_dashboard
from apps.rbac.permission_keys import ROLE_ADMIN

PERF_ASSET_COUNT = 10_500
PERF_BUDGET_SECONDS = 0.8  # T5.5's own budget, looser than F10's 0.5s list/search budget
N_TIMING_RUNS = 5

URL = "/api/v1/dashboard/summary"


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


class TestDashboardSummaryPerf10k:
    def test_cold_and_cached_summary_under_budget(
        self, client, perf_corpus, django_assert_max_num_queries
    ):
        _login(client, perf_corpus.tenant, perf_corpus.admin)

        # Cold (cache-miss) request: still bounded and under budget. Bumps
        # THIS tenant's dashboard cache version (not a blanket `cache.clear()`
        # -- that would also flush the session cache, since sessions live in
        # the same `"default"` cache alias, logging the test client out).
        invalidate_tenant_dashboard(perf_corpus.tenant.id)
        with django_assert_max_num_queries(20):
            elapsed, resp = _timed_get(client, URL)
        assert resp.status_code == 200, resp.content
        assert elapsed < PERF_BUDGET_SECONDS, (
            f"Cold dashboard summary took {elapsed * 1000:.1f} ms at "
            f"{PERF_ASSET_COUNT}+ seeded assets, over the "
            f"{PERF_BUDGET_SECONDS * 1000:.0f} ms budget."
        )
        # Non-retired count is somewhat under the full seeded total (the
        # default totals tile excludes retired assets, same reasoning as
        # `apps.assets.tests.test_perf_10k`'s own list-count assertion).
        assert sum(row["count"] for row in resp.json()["totals_by_category"]) >= 9_000

        # Warm (cached) requests: near-instant, comfortably under budget.
        timings = []
        for _ in range(N_TIMING_RUNS):
            elapsed, resp = _timed_get(client, URL)
            assert resp.status_code == 200
            timings.append(elapsed)
        median = statistics.median(timings)
        assert median < PERF_BUDGET_SECONDS, (
            f"Cached dashboard summary median {median * 1000:.1f} ms exceeds "
            f"the {PERF_BUDGET_SECONDS * 1000:.0f} ms budget. All runs: "
            f"{[round(t * 1000, 1) for t in timings]} ms."
        )
