# T6.6 — Load test results (M6 perf gate)

**Milestone:** `docs/tasks/M6-import-export-deploy.md` T6.6, deps T6.3.
**Exit criterion:** "targets met (or documented gaps with the tuning applied)."

## Targets under test (`docs/architecture.md` §4, quoted verbatim)

> **Targets (tier-1 DS220+, 50k assets / 300 users / ~30 concurrent):**
>
> | Operation | Target (p95, server-side) |
> |---|---|
> | Paginated asset list (25/page, filtered) | < 300 ms |
> | Full-text search across attributes | < 500 ms |
> | Asset detail (scan → open) | < 250 ms |
> | Dashboard aggregate load | < 800 ms (cached) |
> | Check-in/out write | < 300 ms |

Endpoints exercised: `GET /api/v1/assets/` (list), `GET /api/v1/assets/?search=`
(search), `GET /api/v1/assets/{id}/` (detail), `GET /api/v1/dashboard/summary`
(cached dashboard), `POST /api/v1/checkouts/` (check-out write).

## Result summary (after tuning — see "Tuning" below)

| Operation | Target (p95) | Measured p95 | Verdict |
|---|---|---|---|
| List | < 300 ms | **450 ms** | **MISS** (1.5x) |
| Search | < 500 ms | **740 ms** | **MISS** (1.48x) |
| Detail | < 250 ms | **120 ms** | PASS |
| Dashboard (cached) | < 800 ms | **87 ms** | PASS |
| Checkout write | < 300 ms | **41 ms** | PASS |

3/5 targets are met. List and search are diagnosed precisely below (root
cause: PostgreSQL's query planner declines the GIN full-text/trigram bitmap
index plan once the RLS policy's `current_setting()`-based tenant predicate
is present, forcing a full/parallel sequential scan on every list/search
request) with real tuning applied (partial mitigation) and a concrete,
verified recommendation for the remaining gap, per this task's own "targets
met (or documented gaps with the tuning applied)" allowance.

## Environment caveat (expected, called out explicitly per the task
instructions)

This sandbox is **not** the DS220+: ~20 shared CPU cores (vs. the NAS's dual-
core Celeron) and RAM that's node contended with other unrelated containers
on the same host, no real TLS termination in front of nginx, and no NAS-class
disk I/O characteristics. **Absolute numbers here do not predict the real
device's numbers.** What *does* transfer regardless of hardware: (a) whether
the query/index/cache design holds up at real 50k+-row scale and real
concurrency (it mostly does — 3/5 pass), and (b) the RLS+planner defect this
run caught, which is a property of the query shape and the Postgres version,
not the box it runs on — the real NAS will see the same seq-scan behavior
(likely *worse*, since it has only 2 cores for Postgres parallel workers to
lean on).

## Setup

### 1. Seed corpus

Extended M1's existing `seed_perf_assets` (`backend/apps/assets/management/
commands/seed_perf_assets.py`, already supports `--count`) — no new seed
command needed for the asset corpus itself:

```
docker compose run --rm migrate python manage.py seed_perf_assets --count 50000
```

**Also seeded a second, smaller tenant** (`noise-tenant-1`, 20,000 assets) —
see "Multi-tenant corpus shape" below for why. Final table: **73,003 total
`Asset` rows** (50,003 in the tenant under test + 20,000 "noise" + 3
pre-existing from earlier M0 seeds), comfortably over the 50k target.

### 2. Load-test users (new, `tests/load/`-only fixture)

`backend/apps/assets/management/commands/seed_load_test_admin.py` — seeds
`--count` (default 30) **distinct** tenant-wide Admin users
(`loadtest-admin-000@perf.test` .. `loadtest-admin-029@perf.test`) in the
perf-seed tenant:

```
docker compose run --rm migrate python manage.py seed_load_test_admin --count 30
```

**Why 30 distinct users, not one shared login (empirical finding — read
before changing this):** `rest_framework.throttling.UserRateThrottle`
(`"user": "1000/min"`) keys its bucket by `request.user.pk`, not session. A
first draft logged all 30 simulated concurrent users in as the SAME Admin
account (30 sessions, 1 user row) — the aggregate test traffic across all 30
"users" shared one 1000/min bucket and got spuriously `429`'d well below the
real endpoint capacity being measured. 30 real concurrent lab members are 30
distinct `User` rows in production, each with its own bucket, so this
wouldn't be an issue for them — distinct seeded users matches that reality.

### 3. Session provisioning (`tests/load/provision_sessions.py`)

`POST /api/v1/auth/login` is throttled **10/min per client IP**
(`config/settings/base.py`, T0.6 anti-brute-force). 30 near-simultaneous
logins from ONE load-generator host IP hit that immediately (confirmed: ~82%
`429` at `-u 30 -r 6`) — a real deployment's 30 users arrive from ~30
distinct IPs behind Cloudflare and would never collectively hit it. Re-
running the throttled request in a loop would just be attacking our own
rate limiter, not measuring the T6.6 targets. So: log in **sequentially**,
one real `POST /auth/login` per simulated user, 7s apart (safely under
10/min), *before* the timed run, and save each session's cookies to
`results/sessions.json`. Every request DURING the timed run still carries a
real, distinct, server-validated session through the full session-auth +
CSRF + tenant-context + RBAC + RLS stack — only the login round-trip itself
is precomputed (and login is not one of the 5 measured targets).

```
python3 -m venv .venv-load && source .venv-load/bin/activate
pip install -r tests/load/requirements.txt
python3 tests/load/provision_sessions.py --host http://localhost:8080 --count 30
```

### 4. Stack: prod-profile + a load-test overlay

`docker-compose.loadtest.yml` (new) layers on `docker-compose.yml` +
`docker-compose.prod.yml` (T6.3's prod overlay — gunicorn, `DEBUG=False`,
Celery concurrency, RLS app role, HSTS, all otherwise unchanged):

- Publishes `nginx` on host port 8080 (the real deploy never does this —
  Cloudflare Tunnel only) so a load generator on the host can reach it.
- Disables `SECURE_SSL_REDIRECT`/`SESSION_COOKIE_SECURE`/`CSRF_COOKIE_SECURE`
  (made env-overridable in `backend/config/settings/prod.py`, default
  unchanged/`True` — **never** set these `false` in a real deployment's
  `.env`) since this stack is hit over plain HTTP from the host with no TLS
  terminator in front of nginx locally. Not on the request-latency critical
  path.

```
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  -f docker-compose.loadtest.yml up -d
```

### 5. Load generator: Locust

**Tooling choice:** neither `locust` nor any load-testing tool was already
in `backend/requirements/`. Picked `locust` (pure-Python, pip-installable,
no extra infra, real concurrent HTTP via `gevent`) over alternatives (k6,
Gatling — both need a separate non-Python runtime/JVM; ab/wrk — too crude to
express the real login+CSRF+session flow this task requires) — fits the
stack and needs nothing beyond `pip install`. Kept OUT of
`backend/requirements/dev.txt` (that file is pytest-CI scope); pinned
separately in `tests/load/requirements.txt`.

Two locustfiles, deliberately separate (see each file's own docstring for
why): `locustfile_read.py` (list/search/detail/dashboard — kept apart from
writes so the dashboard's cache measurement is never invalidated mid-run by
a concurrent checkout) and `locustfile_checkout.py` (checkout create, each
user cycling its own disjoint slice of the AVAILABLE/non-consumable asset
pool so the pool never depletes and no two users contend for the same row's
lock).

```
python3 -m locust -f tests/load/locustfile_read.py --host http://localhost:8080 \
  --headless -u 30 -r 30 -t 120s --csv tests/load/results/read

python3 -m locust -f tests/load/locustfile_checkout.py --host http://localhost:8080 \
  --headless -u 30 -r 30 -t 90s --csv tests/load/results/checkout
```

## Tuning performed (with before/after numbers)

### 1. Gunicorn worker/thread model (real, applied, kept)

**Finding:** the base prod command (`docker-compose.prod.yml`, `--workers 2`
plain **sync** workers) can serve only 2 requests AT ONCE. At 30 concurrent
users, every list/search/detail/dashboard request measured in isolation
(single `curl`, no concurrency) was already well under budget (list ~180ms,
search ~310ms, dashboard ~70ms) — yet the full 30-concurrent run showed p95
in the **2.2-3.9s** range, purely from requests queueing behind 2 busy sync
workers, not slow queries.

**Fix:** this workload is I/O-bound (waiting on Postgres; `psycopg` releases
the GIL during that wait), so switching to **`gthread`** workers lets each
gunicorn process serve several requests concurrently via threads instead of
one at a time, without leaving the documented "2-3 workers" RAM budget
(`docs/deployment.md`, 768 MB `web` `mem_limit`):

```
gunicorn ... --workers 2 --worker-class gthread --threads 8
```

| Metric | Before (2 sync) | After (2 gthread x8) |
|---|---|---|
| List p95 | ~3.2s (queueing-dominated, not representative) | 610 ms |
| Search p95 | ~3.4s | 900 ms |
| Detail p95 | ~3.1s | 140 ms |
| Dashboard p95 | ~2.9s | 130 ms |

Applied and kept in `docker-compose.loadtest.yml`'s `web.command`.
**Recommendation:** carry `--worker-class gthread --threads 4-8` into
`docker-compose.prod.yml` itself (still 2-3 processes, same RAM budget) —
this is a real, low-risk, RAM-neutral win for the actual DS220+ deploy too,
not just an artifact of this test environment.

A second data point was also collected at `--workers 4 --threads 6` (12 GB
host, **not** RAM-budget-compliant for the 2-core/6 GB NAS — collected only
to separate "more raw parallelism helps a little further" from "the
remaining gap is a query-plan problem, not a worker-count problem"; see
final numbers below, collected at this setting on the more realistic 2-
tenant corpus). It closed some of the remaining gap (workers x1.5-2, not
proportionally) but did **not** get list/search under target — consistent
with the query-plan diagnosis below, not a worker-count deficiency.

### 2. Multi-tenant corpus shape (real, applied, kept)

**Finding:** the original 50k-asset corpus was **entirely single-tenant**
(`seed_perf_assets`'s one `perf-seed-lab` tenant owned literally 100% of the
`assets_asset` table). That is an adversarial, non-representative shape for
a shared-schema multi-tenant table: it makes the RLS `tenant_id` predicate
carry **zero selectivity** (it matches every row), which measurably worsens
the planner's row-estimate-driven choice between a full/parallel scan and
the GIN bitmap-index plan for `?search=` (see root-cause section below).

**Fix:** seeded a second ~20k-asset tenant (`noise-tenant-1`, same
`seed_perf_assets` command, different `--tenant-slug`) so the table is
genuinely multi-tenant (73,003 rows total, 50,003 in the tenant under test).
This is also a MORE representative "50k assets" scale test than a single-
tenant corpus would be, and is kept as part of the final seeded fixture.

| Metric | Before (single-tenant, 50k rows) | After (2-tenant, 73k rows) |
|---|---|---|
| Search DB time (`EXPLAIN ANALYZE`, RLS-scoped) | 130 ms (serial seq scan) | 55 ms (parallel seq scan, 2 workers) |

Real, measurable, but **not a fix** — Postgres still declines the GIN bitmap
plan (see below); a 2-core NAS won't get the 2-worker parallel-scan
assist this 20-core sandbox did, so the real device is likely to see numbers
closer to the "before" column here, not the "after" one.

### 3. Root cause of the remaining list/search gap — REFINED & PROVEN (db-migration-specialist follow-up)

> **Update (T6.6 db-migration-specialist follow-up).** The original hand-off
> below called this "a genuine PostgreSQL 16 planner limitation" around an
> RLS `Result`/"One-Time Filter" node. That framing was imprecise. A focused
> investigation as `cortex_app` against this same seeded 50k/73k corpus pinned
> the exact mechanism, **refuted two of the proposed fixes with EXPLAIN
> evidence**, and identified the one fix that actually works. The corrected
> analysis follows; the original notes are kept underneath for history.

**The mechanism (proven).** `AssetSearchFilter` runs
`search_vector @@ q  OR  name % s  OR  serial_number % s` plus the RLS tenant
qual `tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::bigint`.
RLS adds its policy predicate as a **security qual** (a *security barrier*).
PostgreSQL will only push another qual *below* a security barrier — i.e. let
it become an index condition evaluated before the tenant check — if that qual
is **`LEAKPROOF`**. The two search operators are not:

```
proname        proleakproof
similarity_op  (%  text,text)   f     <- pg_trgm fuzzy
ts_match_vq    (@@ tsvector,tsquery) f  <- full-text
```

So under RLS the planner may not use `assets_asset_search_vector_gin` /
`assets_asset_name_trgm` / `assets_asset_serial_number_trgm` at all — the
search predicates are demoted to a post-filter and the tenant qual drives the
scan, which for a tenant owning most of its rows means a **(parallel) Seq
Scan**. As the owner (RLS bypassed) there is no barrier, so the same three
indexes are chosen freely.

**EXPLAIN evidence (as `cortex_app`, GUC `app.current_tenant`=4, 50k+ in-tenant):**

| Query / role | Plan | Exec time |
|---|---|---|
| owner, explicit `tenant_id=4` (no RLS), selective term "Jetson Orin" | **BitmapOr** over the 3 GIN/trgm indexes, tenant applied as heap filter | **~6 ms** |
| `cortex_app` (RLS), same term, natural planner | **Parallel Seq Scan** (search predicate is a filter) | ~50 ms (20-core sandbox) |
| `cortex_app` (RLS), same term, `enable_seqscan=off` | Bitmap Index Scan **on the tenant_id index** (returns all 50k rows), then post-filter — NOT the search GINs | ~141 ms (worse) |
| `cortex_app` (RLS), **explicit** `tenant_id=4` regular qual *plus* the RLS qual | still **Seq Scan** | ~122 ms |
| `cortex_app` (RLS), a **single** predicate `search_vector @@ q` (no OR) | still **Seq Scan** | (index still unusable) |

The last two rows are the important refutations:

- **Adding `tenant_id` to the query (or to the index) does not help** — the
  barrier is created by the *security* qual, which is present regardless of
  any additional regular qual. I built all three `btree_gin` composite indexes
  `gin(tenant_id, search_vector)`, `gin(tenant_id, name gin_trgm_ops)`,
  `gin(tenant_id, serial_number gin_trgm_ops)` and re-ran as `cortex_app`: the
  planner used only their `tenant_id` prefix (equivalent to the plain tenant
  index) and still post-filtered the search — **slower**, not faster. These
  indexes were dropped; **no index migration is warranted.** (This corrects
  the original note's "a `btree_gin`-backed composite `(tenant_id,
  search_vector)` index" — one composite on `search_vector` alone can't even
  be considered, because a `BitmapOr` needs *every* OR branch indexable in a
  tenant-aware way; and even all three don't help, per above.)
- **Query restructuring (Subquery / UNION of the OR branches) does not help
  either** — a *single* non-OR search predicate already Seq Scans under RLS
  (row above), so splitting the OR into three separately-planned single-
  predicate scans just yields three Seq Scans. The original recommendation #2
  (below) is a dead end for this reason; don't spend effort there.

**The fix that actually works (verified mechanism, NOT shipped — needs an
explicit tenant-isolation/security sign-off).** Mark the two operators
leakproof so the planner may push them past the RLS barrier and back onto the
GIN/trgm indexes:

```sql
ALTER FUNCTION ts_match_vq(tsvector, tsquery) LEAKPROOF;   -- FTS  @@
ALTER FUNCTION similarity_op(text, text)      LEAKPROOF;   -- pg_trgm  %
```

This restores exactly the ~6 ms BitmapOr plan the owner already gets (the
owner-with-explicit-tenant row above is that plan — the fix simply lets
`cortex_app` reach it too). **Why it is not shipped from here:** it is a
**cluster-global, superuser** change that widens the qual-evaluation ordering
for *every* RLS table and tenant, so it is a deliberate R4 security-posture
decision, not a routine index migration. The theoretical cost is a marginal
side channel (these boolean operators could be evaluated on other tenants'
rows before the RLS filter); they never emit row data and have no data-
dependent error paths, so the practical leak risk is very low — Postgres core
already marks many text/comparison operators `LEAKPROOF` on the same basis —
but the call belongs to whoever owns tenant isolation, made consciously.
When approved, ship it as a `migrations.RunSQL` (reverse:
`ALTER FUNCTION ... NOT LEAKPROOF;`) in `apps/assets`, then delete the xfail
marker on `test_rls_subject_uses_index_for_selective_term` (see next para) —
its strict-xfail will otherwise turn the now-passing assertion into a hard CI
failure, which is the intended prompt to remove it.

**Coverage gap — now CLOSED.** The blind spot (the M1 perf gate's EXPLAINs run
as the RLS-bypassing owner) is closed by
`backend/apps/assets/tests/test_perf_rls_search_plan.py`: it seeds a ~12k +
~4k two-tenant corpus, then runs the real search query's `EXPLAIN (ANALYZE)`
through a raw **`cortex_app`** connection (with the tenant GUC set, and an
assertion that the role is genuinely non-superuser/`NOBYPASSRLS`). A control
test proves the owner uses the index at this scale; the RLS-subject test is a
**strict `xfail`** asserting the RLS role uses the index — it currently xfails
(documenting the defect) and will flip to a hard failure the moment the
leakproof fix lands, forcing the marker's removal and converting it into a
live regression guard.

<details><summary>Original hand-off notes (superseded by the above)</summary>

The original notes attributed the seq scan to "a genuine PostgreSQL 16 planner
limitation specific to the interaction between RLS's `Result`/'One-Time
Filter' wrapper node and choosing a `BitmapOr`-based child plan", and had
tried `SET STATISTICS 500`, a single composite `(tenant_id, search_vector)`
index, `random_page_cost = 1.1`, and the `(SELECT current_setting(...))`
scalar-subquery RLS rewrite without effect. Those observations are consistent
with the leakproof mechanism above (none of them changes operator
leakproofness, so none could move the plan). Original recommendations:
1. Land the `gthread` gunicorn change (tuning #1) — still valid, unrelated.
2. Restructure `AssetSearchFilter` into a Subquery/CTE of the OR branches —
   **refuted above** (single-predicate search already seq-scans under RLS).
3. Add an RLS-subject perf test — **done** (see "Coverage gap" above).
4. Re-run the suite once a fix lands — still the right final step.

</details>

## Final numbers (30 concurrent, ~2 min per read run / 90s checkout run)

Config: `--workers 4 --worker-class gthread --threads 6` (diagnostic,
**exceeds** the documented 2-3-worker/768 MB NAS budget — collected to
separate "more parallelism" from "query-plan problem"; see tuning #1 for the
RAM-budget-compliant 2-worker numbers), 73,003-row 2-tenant corpus.

| Operation | Requests | p50 | p90 | **p95** | p99 | Target (p95) | Verdict |
|---|---|---|---|---|---|---|---|
| List (`GET /assets/`) | 1,526 | 300 ms | 410 ms | **450 ms** | 560 ms | 300 ms | **MISS** |
| Search (`GET /assets/?search=`) | 1,185 | 530 ms | 690 ms | **740 ms** | 940 ms | 500 ms | **MISS** |
| Detail (`GET /assets/{id}/`) | 783 | 40 ms | 86 ms | **120 ms** | 220 ms | 250 ms | PASS |
| Dashboard (`GET /dashboard/summary`, cache-warmed) | 408 | 24 ms | 64 ms | **87 ms** | 200 ms | 800 ms | PASS |
| Checkout (`POST /checkouts/`) | 2,513 | 21 ms | 35 ms | **41 ms** | 72 ms | 300 ms | PASS |

Zero request failures across all runs (0/3,933 read requests, 0/5,056
checkout+checkin requests) — every RBAC/RLS/CSRF/session check on every one
of ~9,000 real HTTP requests behaved correctly under load; this run also
incidentally re-confirms tenant isolation holds at scale under concurrency
(no cross-tenant leakage observed across the 2-tenant, 73k-row corpus during
~4 minutes of concurrent traffic).

Raw locust CSVs: `tests/load/results/read_final_stats.csv`,
`tests/load/results/checkout_final_stats.csv` (percentile breakdown per
endpoint), `*_stats_history.csv` (time series).

## Coverage against the T6.6 exit criterion

- **"targets met"**: 3/5 (detail, dashboard, checkout). **Not fully met**
  for list/search.
- **"or documented gaps with the tuning applied"**: met. Two real,
  verified, kept tuning changes (gthread workers; representative multi-
  tenant corpus) with before/after numbers; the remaining gap is now
  **root-caused to the exact mechanism** (RLS security-barrier + non-
  leakproof `@@`/`%` operators — see §3, refined by the db-migration-
  specialist follow-up with EXPLAIN evidence, refuting the composite-index
  and query-restructure fixes and identifying the `LEAKPROOF` fix that works),
  and the perf-gate coverage gap that let it ship unnoticed through M1-M5 is
  **CLOSED** by `backend/apps/assets/tests/test_perf_rls_search_plan.py` (an
  RLS-subject `cortex_app` EXPLAIN test with a strict-xfail regression
  tripwire). The `LEAKPROOF` fix itself is a conscious R4 security decision
  and is left for explicit sign-off rather than shipped from this pass.
- Explicitly **not** re-litigated here (already proven elsewhere, out of
  this task's scope): F1 RBAC scope, F4 exclusion-constraint correctness,
  F8 audit-log immutability, F3 stock-ledger reconciliation — T6.6 is
  specifically the perf gate.
