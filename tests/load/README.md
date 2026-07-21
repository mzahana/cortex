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

### 3. Root cause of the remaining list/search gap (diagnosed, NOT fixed here — hand-off)

**This is the actual, specific finding this task exists to catch** (per the
task's own framing: "Dry-run this against M1 early to catch index/query
issues").

`AssetSearchFilter` (`backend/apps/assets/api.py`) combines full-text
(`search_vector @@ ...`, GIN) and `pg_trgm` fuzzy matching (`name %`,
`serial_number %`, both GIN) with `OR`, plus the RLS-injected tenant
predicate. Verified via `EXPLAIN (ANALYZE, BUFFERS)` at **every** privilege
level:

- **As the DB owner (`cortex`, RLS bypassed — this is what
  `apps/assets/tests/test_perf_10k.py`'s `perf_corpus` fixture and pytest-
  django's own connection ALSO run as, per that module's own docstring):**
  Postgres correctly picks `BitmapOr` across the 3 GIN indexes —
  **2.4-3.1 ms**.
- **As the real runtime role (`cortex_app`, RLS enabled — i.e. every actual
  HTTP request in production):** Postgres instead picks a **sequential
  scan** over the tenant's entire asset set (serially ~130 ms at 46.5k rows
  in-tenant; parallel with 2 workers ~55 ms at the wider 2-tenant corpus) —
  **40-50x slower**, and this is BEFORE the identical `COUNT(*)` query
  DRF's pagination also runs.

  Confirmed this is specifically about the RLS-injected `Result` / "One-Time
  Filter" plan node (not row-estimate quality, not a missing index): none of
  the following changed the outcome —
  - `ALTER TABLE ... SET STATISTICS 500` + `ANALYZE` (better row estimates)
  - A `btree_gin`-backed composite `(tenant_id, search_vector)` index
  - `SET random_page_cost = 1.1` (SSD-appropriate)
  - Rewriting the RLS policy's `current_setting()` call as a scalar
    subquery (`(SELECT current_setting(...))`), a documented Postgres RLS
    workaround for exactly this class of problem

  This looks like a genuine PostgreSQL 16 planner limitation specific to
  the interaction between RLS's `Result`/"One-Time Filter" wrapper node and
  choosing a `BitmapOr`-based child plan, not a schema/index gap in this
  codebase per se — every index T1.3 built (`assets_asset_search_vector_gin`,
  `assets_asset_name_trgm`, `assets_asset_serial_number_trgm`) is present,
  valid, and does get used the moment RLS is out of the picture.

**Recommendation for backend-engineer / db-migration-specialist:**
1. Immediate, low-risk: land the `gthread` gunicorn change (tuning #1 above)
   — real, if partial, win.
2. Investigate restructuring `AssetSearchFilter`'s query so the 3-way OR
   search predicate is evaluated in a sub-query/CTE the planner can commit
   to a bitmap plan for BEFORE the RLS/tenant filter is applied as an outer
   filter (e.g. `Asset.objects.filter(id__in=Subquery(...))` composed from
   three separately-planned GIN lookups) — needs a `code-reviewer`-gated
   change, not something to land from a QA pass.
3. **Close the coverage gap this hunt exposed**: `apps/assets/tests/
   test_perf_10k.py`'s perf assertions run on pytest-django's DB connection,
   which (per that module's own docstring) is the OWNER/superuser role —
   **RLS is bypassed for that entire test module**, so it structurally
   cannot catch this exact regression class (an RLS-vs-planner interaction)
   no matter how large its corpus gets. Recommend adding at least one
   perf-gate test that goes through a *real* HTTP request as the RLS-subject
   `cortex_app` role (e.g. via the Django test client against a
   `LiveServerTestCase`, or an `EXPLAIN` assertion run through
   `apps.tenancy.context.tenant_context` + the app role connection) so this
   class of defect is caught in CI before the next M-milestone's perf gate,
   not just at the M6 load-test finish line.
4. Re-run this load suite once (2) lands to confirm list/search close under
   target; the checkout/detail/dashboard results already meet target and
   don't need re-verification unless the query shape there also changes.

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
  tenant corpus) with before/after numbers; the remaining gap is
  root-caused to a specific, reproducible Postgres RLS-vs-planner
  interaction (not a vague "it's just slow"), with a concrete recommendation
  and a named coverage gap in the existing perf-gate test suite that let it
  ship unnoticed through M1-M5.
- Explicitly **not** re-litigated here (already proven elsewhere, out of
  this task's scope): F1 RBAC scope, F4 exclusion-constraint correctness,
  F8 audit-log immutability, F3 stock-ledger reconciliation — T6.6 is
  specifically the perf gate.
