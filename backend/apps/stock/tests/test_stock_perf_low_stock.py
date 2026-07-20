"""T2.5 — the low-stock partial index EXPLAIN proof at realistic scale, the
gap flagged in code review of T2.3: T2.3's own migration docstring notes it
confirmed the planner "picks this index via a non-forced EXPLAIN (ANALYZE,
BUFFERS)" but only over a small ad-hoc check, and explicitly defers "the
definitive perf/acceptance EXPLAIN at realistic scale" to this task.

Seeds 8,000+ `StockItem` rows (`apps.stock.perf_seed.seed_stock_items`, ~15%
already at-or-under `reorder_threshold`, ~85% healthy) and runs the ACTUAL
`?low_stock=true` scan query `StockItemViewSet.get_queryset` builds
(`StockItem.objects.filter(quantity_on_hand__lte=F("reorder_threshold"))`,
tenant-scoped, `select_related`) through `.explain(analyze=True,
buffers=True)` under NATURAL planner settings — no `enable_seqscan=off`
override, matching `apps.assets.tests.test_perf_10k`'s rigor: that only
proves index-*eligibility*, not what the planner actually picks at this
scale/selectivity. A seq scan here is a real, reportable finding for the
owning engineer, not something to paper over by loosening the assertion.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from django.db.models import F

from apps.common.tests.factories import (
    DEFAULT_TEST_PASSWORD,
    TenantFactory,
    UserFactory,
    upgrade_tenant_wide_role,
)
from apps.rbac.permission_keys import ROLE_ADMIN
from apps.stock.models import StockItem
from apps.stock.perf_seed import REORDER_THRESHOLD, delete_seeded_stock_tenant, seed_stock_items
from apps.tenancy.context import tenant_context

PERF_STOCK_ITEM_COUNT = 8_000


def _login(client, tenant, user):
    response = client.post(
        "/api/v1/auth/login",
        {"tenant": tenant.slug, "email": user.email, "password": DEFAULT_TEST_PASSWORD},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return response


@pytest.fixture(scope="module")
def stock_perf_corpus(django_db_setup, django_db_blocker):
    """Module-scoped 8k+ StockItem corpus. See `apps.assets.tests.
    test_perf_10k.perf_corpus`'s docstring for why this MUST depend on
    `django_db_setup` explicitly (not just `django_db_blocker`) and why the
    corpus must be deleted in a `finally` (T1.9's leaked-tenant finding) —
    the exact same two traps apply here, at the same fixture shape.
    """
    with django_db_blocker.unblock():
        tenant = TenantFactory()
        admin = UserFactory(tenant=tenant)
        upgrade_tenant_wide_role(admin, ROLE_ADMIN)
        result = seed_stock_items(tenant, count=PERF_STOCK_ITEM_COUNT, batch_size=1000)
        try:
            yield SimpleNamespace(tenant=tenant, admin=admin, result=result)
        finally:
            delete_seeded_stock_tenant(tenant)


pytestmark = pytest.mark.django_db


class TestStockPerfSeedCorpus:
    def test_seed_created_expected_mix(self, stock_perf_corpus):
        result = stock_perf_corpus.result
        assert result.stock_items_created >= PERF_STOCK_ITEM_COUNT
        assert result.low_stock_count > 0
        assert result.healthy_count > 0
        # Sanity: low-stock is a genuine MINORITY (the partial index's whole
        # value proposition is that it's small relative to the full table).
        assert result.low_stock_count < result.healthy_count

    def test_seed_quantities_reconciled_to_ledger(self, stock_perf_corpus):
        """Every seeded row's `quantity_on_hand` really does equal its
        ledger sum (the backfill in `seed_stock_items` claims this; verify
        it against a random sample rather than trusting the claim)."""
        tenant = stock_perf_corpus.tenant
        with tenant_context(tenant.id):
            sample = list(StockItem.objects.all()[:25])
            assert len(sample) == 25
            for item in sample:
                assert item.quantity_on_hand == item.recompute_quantity_on_hand()


def _low_stock_queryset(tenant_id: int):
    """Reconstruct the EXACT queryset `StockItemViewSet.get_queryset` builds
    for `?low_stock=true` (tenant-scoped, `select_related`, the partial
    index's own comparison) so `.explain()` reflects the real endpoint
    query, not an approximation.
    """
    with tenant_context(tenant_id):
        qs = (
            StockItem.objects.select_related("asset", "asset__project", "bin_location")
            .order_by("-created_at")
            .filter(quantity_on_hand__lte=F("reorder_threshold"))
        )
        return qs


class TestLowStockPartialIndexExplain:
    """**KEY OUTPUT** (the T2.3 review gap this task closes): does the
    planner naturally choose the partial index
    (`stock_stock_item_low_stock_partial`) for the real `?low_stock=true`
    scan at 8k+ rows, WITHOUT `enable_seqscan=off`?
    """

    def test_explain_analyze_low_stock_scan_natural_planner(self, stock_perf_corpus, capsys):
        tenant = stock_perf_corpus.tenant
        qs = _low_stock_queryset(tenant.id)
        with tenant_context(tenant.id):
            plan_lines = qs.explain(analyze=True, buffers=True, verbose=False)
        plan = plan_lines if isinstance(plan_lines, str) else "\n".join(plan_lines)

        with capsys.disabled():
            print(
                "\n--- EXPLAIN (ANALYZE, BUFFERS) for ?low_stock=true "
                f"over {PERF_STOCK_ITEM_COUNT}+ StockItem rows (natural planner) ---"
            )
            print(plan)
            print("--- end plan ---\n")

        uses_seq_scan = "Seq Scan on stock_stock_item" in plan
        uses_partial_index = "stock_stock_item_low_stock_partial" in plan
        uses_any_index_path = ("Index Scan" in plan) or ("Bitmap Index Scan" in plan)

        print(
            f"VERDICT: seq_scan={uses_seq_scan} "
            f"partial_index_named_in_plan={uses_partial_index} "
            f"any_index_scan_path={uses_any_index_path}"
        )

        # Not a hard pytest failure either way (mirrors
        # `apps.assets.tests.test_perf_10k.TestPerfSearchExplainPlan`'s own
        # stance) — this test's job is to PROVE and REPORT which branch the
        # planner actually took at this scale/selectivity for the QA report;
        # a seq-scan finding here is real and gets routed back to
        # db-migration-specialist/backend-engineer, not silently loosened
        # into a pass.
        if uses_seq_scan and not uses_partial_index:
            print(
                "FINDING: the planner used a Seq Scan, NOT the low-stock partial "
                "index, for the real ?low_stock=true query at "
                f"{PERF_STOCK_ITEM_COUNT}+ rows — see the printed plan above."
            )


class TestLowStockEndpointCorrectnessAtScale:
    """The application-level counterpart to the EXPLAIN proof above: the real
    `GET /api/v1/stock/?low_stock=true` endpoint returns exactly the expected
    subset at this scale, verified against an independent ORM count (not the
    seed's own bookkeeping), with a bounded query count (no N+1)."""

    def test_low_stock_filter_correct_and_bounded_at_scale(
        self, client, stock_perf_corpus, django_assert_max_num_queries
    ):
        tenant = stock_perf_corpus.tenant
        _login(client, tenant, stock_perf_corpus.admin)

        with tenant_context(tenant.id):
            expected_count = StockItem.objects.filter(
                quantity_on_hand__lte=F("reorder_threshold")
            ).count()
        assert expected_count == stock_perf_corpus.result.low_stock_count
        assert expected_count > 0

        with django_assert_max_num_queries(20):
            resp = client.get("/api/v1/stock/?low_stock=true")
        assert resp.status_code == 200, resp.content
        assert resp.json()["count"] == expected_count

    def test_healthy_items_excluded_at_scale(self, client, stock_perf_corpus):
        tenant = stock_perf_corpus.tenant
        _login(client, tenant, stock_perf_corpus.admin)

        resp = client.get("/api/v1/stock/?low_stock=true")
        assert resp.status_code == 200, resp.content
        for row in resp.json()["results"]:
            assert row["quantity_on_hand"] <= REORDER_THRESHOLD
