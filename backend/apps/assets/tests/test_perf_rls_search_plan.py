"""T6.6 follow-up — the perf-gate coverage gap the M6 load test exposed.

## Why this module exists

`apps.assets.tests.test_perf_10k` is the M1 perf gate. Its `.explain()`
assertions run on pytest-django's **`default`** connection, which is the
migration/table-OWNER role (`cortex`, `rolsuper=t rolbypassrls=t`). Postgres
does **not** apply RLS policies to the table owner, so every EXPLAIN in that
module sees a plan with **no tenant predicate at all** — it structurally
cannot observe how the planner behaves once the RLS `tenant_id` security qual
is injected, which is what every real HTTP request actually runs as (the
non-superuser `cortex_app` role).

The M6 load test (`tests/load/README.md`) caught exactly that blind spot: at
50k rows the `?search=` query does a fast `BitmapOr` over the GIN/trgm indexes
as the owner (~6 ms) but a **sequential scan** as `cortex_app` (~50-130 ms),
because the RLS security qual is a security *barrier* and the `@@`/`%` search
operators are **not leakproof**, so the planner may not push them below the
barrier to become index conditions. The full root-cause analysis and the
verified fix (marking `ts_match_vq` / `similarity_op` `LEAKPROOF`, a
cluster-global change pending explicit sign-off) live in `tests/load/README.md`
§3.

This module closes that gap: it runs the **exact** endpoint search query's
`EXPLAIN (ANALYZE)` through the real, non-superuser, RLS-subject `cortex_app`
connection (mirroring `apps.tenancy.tests.test_cortex_app_runtime_rls` and
`backend/conftest.py::app_role_connection`) at index-worthy scale, so this
class of RLS-vs-planner regression is visible in CI on the role production
actually uses — not just on the owner role that bypasses RLS.

## Corpus

Module-scoped, committed via `django_db_blocker.unblock()` (same pattern and
same `finally`-teardown rationale as `test_perf_10k.perf_corpus`): a
~12k-asset target tenant plus a smaller ~4k-asset "noise" tenant, so the
`tenant_id` predicate carries realistic (non-100%) selectivity — the
single-tenant-only shape the load test flagged as adversarial/non-
representative for a shared-schema multi-tenant table.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import psycopg
import pytest
from django.db import connection

from apps.assets.perf_seed import delete_seeded_tenant, seed_assets
from apps.common.tests.factories import TenantFactory
from apps.tenancy.db import APP_TENANT_GUC

TARGET_TENANT_ASSETS = 12_000
NOISE_TENANT_ASSETS = 4_000

# Verified reliably selective (~3% of the corpus) and proven to make the OWNER
# planner pick a BitmapOr over the GIN/trgm indexes at scale — so if the
# RLS-subject role does NOT get an index path for THIS term, that is the RLS
# security-barrier effect, not merely a small-table/non-selective seq scan.
SELECTIVE_TERM = "RTX 4090"


def _app_role_dsn() -> str:
    """Raw DSN for the non-superuser, RLS-subject `cortex_app` role against the
    SAME test database pytest-django migrated — mirrors
    `backend/conftest.py::_app_role_dsn`.
    """
    params = connection.get_connection_params()
    return psycopg.conninfo.make_conninfo(
        host=params.get("host") or "localhost",
        port=params.get("port") or 5432,
        dbname=connection.settings_dict["NAME"],
        user=os.environ.get("APP_DB_USER", "cortex_app"),
        password=os.environ.get("APP_DB_PASSWORD", "changeme-app-db-password"),
    )


# The EXACT filtered/ordered/limited SELECT `AssetViewSet` list + `AssetSearchFilter`
# runs for `?search=<term>` (retired excluded, the 3-way FTS/trgm OR, rank
# ordering, one page). Parameterised so the same text drives the owner and the
# cortex_app EXPLAIN — the only difference between the two runs is which role
# (and therefore whether RLS injects its tenant qual), which is the whole point.
_SEARCH_SQL = """
EXPLAIN (ANALYZE, FORMAT JSON)
SELECT a.id
FROM assets_asset a
WHERE NOT ((a.status)::text = 'retired')
  AND (
        a.search_vector @@ websearch_to_tsquery('english', %(term)s)
        OR (a.name)::text %% %(term)s
        OR (a.serial_number)::text %% %(term)s
  )
ORDER BY ts_rank(a.search_vector, websearch_to_tsquery('english', %(term)s)) DESC,
         a.created_at DESC
LIMIT 25
"""


def _node_types(plan_node: dict) -> list[str]:
    """Flatten every ``Node Type`` in an EXPLAIN (FORMAT JSON) plan tree."""
    types = [plan_node["Node Type"]]
    for child in plan_node.get("Plans", []):
        types.extend(_node_types(child))
    return types


def _explain_as_owner(term: str) -> list[str]:
    """EXPLAIN the search query on Django's `default` connection = the table
    OWNER (RLS bypassed). This is the CONTROL: it proves the GIN/trgm indexes
    are effective for `term` at this corpus scale.
    """
    with connection.cursor() as cur:
        cur.execute(_SEARCH_SQL, {"term": term})
        plan = cur.fetchone()[0]
    return _node_types(plan[0]["Plan"])


def _explain_as_cortex_app(term: str, tenant_id: int) -> tuple[list[str], str]:
    """EXPLAIN the SAME query on a raw `cortex_app` connection with the tenant
    GUC set — i.e. exactly as a real HTTP request runs, with RLS active.
    Returns (node types, pretty JSON plan) so the plan can be printed as CI
    evidence.
    """
    import json

    conn = psycopg.connect(_app_role_dsn())
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT set_config(%s, %s, false)", [APP_TENANT_GUC, str(tenant_id)])
            # Sanity: this really is the RLS-subject role, not a superuser that
            # would silently bypass RLS and make this whole test a false pass.
            cur.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            role_row = cur.fetchone()
            assert role_row is not None
            rolsuper, rolbypassrls = role_row
            assert rolsuper is False and rolbypassrls is False, (
                "EXPLAIN ran as a superuser/bypassrls role — RLS would be bypassed "
                "and this test could not observe the security-barrier effect."
            )
            cur.execute(_SEARCH_SQL, {"term": term})
            plan_row = cur.fetchone()
            assert plan_row is not None
            plan = plan_row[0]
    finally:
        conn.close()
    return _node_types(plan[0]["Plan"]), json.dumps(plan[0]["Plan"], indent=2)


@pytest.fixture(scope="module")
def rls_perf_corpus(django_db_setup, django_db_blocker):
    """~12k-asset target tenant + ~4k-asset noise tenant, committed once.

    See `test_perf_10k.perf_corpus` for why this MUST depend on
    `django_db_setup` (not just `django_db_blocker`) and why both tenants are
    deleted in a `finally` (T1.9's leaked-tenant trap).
    """
    with django_db_blocker.unblock():
        target = TenantFactory()
        noise = TenantFactory()
        seed_assets(target, count=TARGET_TENANT_ASSETS, batch_size=1000, rng_seed=42)
        seed_assets(noise, count=NOISE_TENANT_ASSETS, batch_size=1000, rng_seed=7)
        try:
            yield SimpleNamespace(target=target, noise=noise)
        finally:
            delete_seeded_tenant(noise)
            delete_seeded_tenant(target)


pytestmark = pytest.mark.django_db


class TestRlsSubjectSearchPlan:
    """The gap-closer: prove which plan the planner picks for the real search
    query on the OWNER role vs. the RLS-subject `cortex_app` role, at the same
    index-worthy scale, so the M1 perf gate's owner-only blind spot can't hide
    an RLS-vs-planner regression again.
    """

    def test_owner_control_uses_index_for_selective_term(self, rls_perf_corpus, capsys):
        """CONTROL (owner, RLS bypassed): the GIN/trgm indexes ARE chosen for a
        selective term at this scale. If this fails, the corpus is too small /
        the term too common for the RLS-subject assertion below to be
        meaningful — so this is a hard assertion.
        """
        node_types = _explain_as_owner(SELECTIVE_TERM)
        with capsys.disabled():
            print(f"\n[owner, RLS bypassed] ?search={SELECTIVE_TERM!r} plan nodes: {node_types}")
        uses_index = any("Bitmap" in n or "Index Scan" in n for n in node_types)
        assert uses_index, (
            "The owner-role plan did NOT use an index path for a known-selective "
            f"term at {TARGET_TENANT_ASSETS}+ rows (nodes={node_types}); the corpus "
            "is not at index-worthy scale, so the RLS-subject comparison below "
            "would not be meaningful."
        )

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "KNOWN, DOCUMENTED DEFECT (tests/load/README.md §3): the RLS tenant qual "
            "is a security barrier and the search operators `@@`/`%` are not leakproof, "
            "so the planner cannot push them below the barrier to become index "
            "conditions — the RLS-subject role SEQ-SCANS instead of using the GIN/trgm "
            "indexes. This xfail is the regression tripwire: it will start PASSING "
            "(-> strict xfail turns that into a hard failure, forcing removal of this "
            "marker) the moment the LEAKPROOF fix lands, converting this into a live "
            "guard against the seq-scan ever silently returning."
        ),
    )
    def test_rls_subject_uses_index_for_selective_term(self, rls_perf_corpus, capsys):
        """THE GAP-CLOSER: the SAME selective search, run as the real
        `cortex_app` RLS-subject role, should use the GIN/trgm index path just
        like the owner control does — currently it does NOT (see xfail reason).
        """
        node_types, pretty_plan = _explain_as_cortex_app(SELECTIVE_TERM, rls_perf_corpus.target.id)
        with capsys.disabled():
            print(
                f"\n[cortex_app, RLS active] ?search={SELECTIVE_TERM!r} "
                f"plan nodes: {node_types}\n{pretty_plan}"
            )
        seq_scan = "Seq Scan" in node_types
        uses_index = any("Bitmap" in n or "Index Scan" in n for n in node_types)
        print(f"VERDICT (cortex_app): seq_scan={seq_scan} uses_index={uses_index}")
        assert uses_index and not seq_scan, (
            "The RLS-subject role seq-scanned the search query instead of using the "
            f"GIN/trgm indexes (nodes={node_types}) — see tests/load/README.md §3."
        )
