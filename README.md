# Cortex — Lab Asset & Inventory Management Platform

Cortex tracks a robotics lab's physical assets — compute/GPUs, edge devices,
drones, components, tools, instruments — end to end: what the lab owns,
where it is, who has it, when it's due back, and when consumables run low.
It's mobile-first (installable PWA, camera QR scan, printable asset labels)
and multi-tenant (each lab/team is an isolated tenant on shared
infrastructure).

**Status: MVP complete** (milestones M0–M6). See [`CHANGELOG.md`](CHANGELOG.md)
for what shipped in each milestone, and [`docs/risks.md`](docs/risks.md) §3
for the handful of open product decisions (exact deploy hostname, Avery
label stock, a real import spreadsheet sample) still pending before a live
deploy is fully tuned to your lab.

## What it does

| Area | Capability |
|---|---|
| Asset registry | Heterogeneous assets with typed custom fields per category, full-text + fuzzy search, tags, projects |
| Consumables | Ledger-backed stock tracking, low-stock alerts, reorder workflow |
| Reservations & checkout | Calendar booking with conflict detection, approval routing, check-in/out with overdue tracking |
| Mobile scan & labels | Installable PWA, camera QR scan → asset, camera photo capture, printable Avery-sheet QR labels |
| Notifications | Async email (Brevo) for reservations, approvals, overdue/low-stock reminders, per-user preferences |
| Audit & dashboard | Immutable audit trail, live dashboard tiles, role-scoped visibility |
| Import/export | Bulk CSV/Excel import with dry-run validation, filtered CSV export |

Full feature list with acceptance criteria: [`docs/features.md`](docs/features.md).

## Stack

- **Backend:** Django 5 + Django REST Framework, PostgreSQL 16 (Row-Level
  Security enforces tenant isolation at the database layer), Redis (cache +
  sessions + Celery broker), Celery + beat for async/scheduled work, Argon2
  password hashing.
- **Frontend:** React + TypeScript + Vite, built as an installable PWA
  (Mantine UI, `@zxing/browser` for QR scanning).
- **Labels:** WeasyPrint + segno render QR label PDFs server-side, in Celery.
- **Email:** Brevo, behind an `EmailProvider` interface (swappable).
- **Media:** `django-storages`, filesystem-backed volume by default,
  swappable to S3-compatible object storage via env config only.
- **Infra:** nginx (reverse proxy + static/media), cloudflared (outbound-only
  Cloudflare Tunnel — no inbound router ports), all services orchestrated by
  `docker-compose.yml`.

See [`docs/architecture.md`](docs/architecture.md) for the full design and
the reasoning behind these choices.

## Repository layout

```
.
├── backend/              # Django + DRF project
│   ├── apps/             # one Django app per domain area (see below)
│   └── config/           # settings (base/dev/test/prod), urls, celery
├── frontend/             # React + TS + Vite PWA (src/screens/, src/api/)
├── docker/               # Dockerfile (shared web/worker/beat image), nginx
│                         #  conf, cloudflared, backup script
├── docker-compose.yml         # dev/base topology (all 7 services)
├── docker-compose.prod.yml    # prod overlay (DEBUG=false, worker tuning)
├── docs/                 # design docs — source of truth, read before coding
│   ├── deployment.md          # design-level deploy overview
│   ├── deployment-runbook.md  # step-by-step operator runbook (NAS/Tunnel/DNS)
│   └── tasks/                 # milestone task breakdown (M0–M6, done)
├── tests/load/            # Locust load test + results (50k-asset perf gate)
├── .env.example           # every documented env key, placeholder values only
└── CLAUDE.md               # working agreement for AI coding agents
```

**Backend apps** (`backend/apps/`), one per domain area: `tenancy` (tenant
core + RLS helpers), `accounts` (users), `rbac` (roles/permissions/
memberships), `catalog` (categories/locations/custom fields), `projects`,
`assets` (registry, search, attachments, QR resolver), `stock` (consumables
ledger), `reservations` (booking + checkout), `labels` (QR label PDFs),
`jobs` (generic async-job polling, backs labels/imports), `imports` (bulk
CSV/Excel import + CSV export), `notifications` (email), `audit`,
`dashboard`, `common` (shared test factories/utilities).

## Getting started (local dev)

```bash
cp .env.example .env              # fill in real values; never commit .env
docker compose up -d               # postgres, redis, migrate, web, worker,
                                    # beat, nginx (cloudflared no-ops locally
                                    # without a real TUNNEL_TOKEN)
```

`migrate` runs automatically before `web` starts — no manual migration step
needed on first boot. The app is then reachable at `http://localhost` via
nginx (add a port mapping in a local `docker-compose.override.yml` if you
need one, e.g. `nginx: {ports: ["8080:80"]}`).

Frontend dev server with hot reload (proxies `/api` to the backend):

```bash
cd frontend && npm install && npm run dev
```

Create your first tenant + admin user — see
[`docs/deployment-runbook.md`](docs/deployment-runbook.md) §3c for the exact
commands (must run inside `tenant_context(...)`, since the app connects as a
non-superuser RLS-subject role — a naive `create_superuser()` call will be
rejected by Row-Level Security).

### Running tests

The `web`/`worker`/`beat` image bakes in `backend/` source at build time
(no bind mount) — **after any backend code change, rebuild before testing**:

```bash
docker compose build web
docker compose up -d postgres redis
docker compose run --rm --no-deps -u root \
  -e DJANGO_SETTINGS_MODULE=config.settings.test \
  -e DATABASE_URL="$(grep '^DATABASE_URL=' .env | cut -d= -f2-)" \
  web sh -c "pip install -r requirements/dev.txt -q && pytest -q"
```

Frontend: `cd frontend && npm run typecheck && npm run lint && npm run build`.

## Deploying

Cortex is designed to self-host on a small NAS (reference target: Synology
DS220+) behind a Cloudflare Tunnel (no inbound ports opened, TLS terminated
at the edge). To deploy:

1. Read [`docs/deployment.md`](docs/deployment.md) for the design-level
   overview (topology, memory budget, hardening checklist).
2. Follow [`docs/deployment-runbook.md`](docs/deployment-runbook.md) for the
   exact, copy-pasteable steps: Cloudflare Tunnel + DNS + SSL setup,
   SPF/DKIM/DMARC records for email deliverability, Synology Container
   Manager bring-up, and first-boot tenant/admin creation. Bring the stack
   up with the production overlay: `docker compose -f docker-compose.yml -f
   docker-compose.prod.yml up -d`.
3. Set up backups: `docker/backup/backup.sh` (nightly `pg_dump`, daily+weekly
   rotation) — wire it into your scheduler per the runbook's Backups section.
   **Test your restore before you need it** — the runbook documents a real,
   verified restore drill to follow.

## Documentation map

Read in this order before making changes: [`docs/overview.md`](docs/overview.md),
[`docs/architecture.md`](docs/architecture.md), [`docs/data-model.md`](docs/data-model.md),
[`docs/rbac.md`](docs/rbac.md), [`docs/api-and-ui.md`](docs/api-and-ui.md),
[`docs/features.md`](docs/features.md), [`docs/deployment.md`](docs/deployment.md),
[`docs/roadmap.md`](docs/roadmap.md), [`docs/risks.md`](docs/risks.md).
The (now-complete) build plan lives in [`docs/tasks/`](docs/tasks/) — one
file per milestone (M0–M6), each with per-task exit criteria; useful as a
map of what was built and why, and as a template for how future work in
this repo should be scoped and verified.

## Non-negotiable invariants

See [`CLAUDE.md`](CLAUDE.md) at the repo root for the full list (tenant
isolation via a central scoped manager + Postgres RLS backstop, server-side
RBAC on every endpoint, audit logging on mutations, 12-factor config, no
local state, no secrets in the image/git, server-side pagination). These
apply to every change, not just new features.

## Known, accepted gaps (MVP)

- **On-device mobile verification** (camera QR scan, photo capture) hasn't
  been exercised on a real phone yet — it needs the live HTTPS Tunnel deploy
  (secure context) to test. Automated coverage (resolver, QR round-trip
  decode, PWA installability) is in place; see `docs/tasks/M4-mobile-scan-labels.md`.
- **Asset list/search performance at 50k+ rows** misses its p95 latency
  target under load. Root cause: PostgreSQL's Row-Level Security enforces
  the tenant filter as a security barrier, and the search operators aren't
  marked `LEAKPROOF`, so the planner can't use the search indexes below that
  barrier. The fix would override a deliberate upstream PostgreSQL security
  decision cluster-wide — deliberately not applied; see `tests/load/README.md`
  for the full analysis and `backend/apps/assets/tests/test_perf_rls_search_plan.py`
  for the regression tripwire that guards it.
- A few open product questions remain in `docs/risks.md` §3 (exact deploy
  hostname/sender domain, Avery label stock, a representative import
  spreadsheet) — the code uses documented, flagged defaults for each until
  answered.
