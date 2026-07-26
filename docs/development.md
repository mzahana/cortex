# Developer guide — local dev, tests, rebuilds

This guide is for **developers** working on Cortex. If you only want to
install and run Cortex, start with the [README](../README.md); to deploy it
for a team, follow the [deployment runbook](deployment-runbook.md). This page
collects the technical detail those two intentionally leave out.

See also [`CLAUDE.md`](../CLAUDE.md) for the non-negotiable invariants and the
hard-won operational gotchas that apply to every change.

## Prerequisites

- Docker + Docker Compose v2, git.
- Node.js 20+ **only** if you use the hot-reload dev server (below) or run
  frontend lint/typecheck locally. A normal containerized run builds the
  frontend for you — no Node install needed.

## First run

Follow the README's "Run it on your computer" steps (clone, `cp .env.example
.env`, fill in the `changeme-*` values, `docker compose build`, `docker
compose up -d`). The notes below explain the parts a developer needs beyond
that happy path.

### The `.env` values that matter for local dev

The defaults in `.env.example` for `ALLOWED_HOSTS` / `CSRF_TRUSTED_ORIGINS` /
`DJANGO_SETTINGS_MODULE` etc. are fine as-is for local dev, except:

- `SECRET_KEY` — generate one:
  `python3 -c "import secrets; print(secrets.token_urlsafe(50))"`.
- `POSTGRES_PASSWORD` / `DATABASE_URL`, and `APP_DB_PASSWORD` /
  `APP_DATABASE_URL` — pick real (even if only locally-real) passwords; keep
  each password consistent between the two vars that reference it.
- `BREVO_API_KEY` / `TUNNEL_TOKEN` — can stay as placeholders for local dev.
  Email sends will fail (logged, not fatal) and `cloudflared` will idle
  without a real tunnel token — neither blocks the rest of the stack.

`.env.example`'s inline comments document every variable. `deployment.md`
explains the reasoning behind the two separate DB roles (`cortex` owner vs.
`cortex_app` RLS-subject runtime role) and other non-obvious choices.

### Why there is a `migrate` service

`docker compose up -d` starts all 7 services: `postgres`, `redis`, `migrate`
(a one-off that runs `manage.py migrate --noinput` and exits — no manual
migration step needed), then `web`, `worker`, `beat`, `nginx`. `cloudflared`
restart-loops harmlessly without a real `TUNNEL_TOKEN`.

### Exposing a host port for browser access

nginx publishes no host port by default (it's designed to sit behind the
Cloudflare Tunnel in production). For local browser access, add a port
mapping via `docker-compose.override.yml` (compose loads this file
automatically — no extra flag). Example is provided at the repo root:

```yaml
services:
  nginx:
    ports:
      - "8080:80"
```

then browse `http://localhost:8080`.

> **CSRF note:** `CSRF_TRUSTED_ORIGINS=http://localhost` (the `.env.example`
> default) does **not** match `http://localhost:8080`. If you publish nginx on
> a non-default port, add the port-specific origin to `.env` temporarily and
> revert it afterward.

### Frontend dev server (hot reload)

For active frontend work — proxies `/api` to the backend so you don't rebuild
`dist/` on every change:

```bash
cd frontend && npm install && npm run dev
```

## Creating a tenant + admin

There is no signup flow by design (multi-tenant, invite-only). The exact
copy-paste shell snippet — which must run inside `tenant_context(...)` because
the app connects as the non-superuser `cortex_app` RLS-subject role — lives in
the deployment runbook, [§3c](deployment-runbook.md#3c-first-time-bring-up),
and works unchanged locally. Run it against the local stack with:

```bash
docker compose exec web python manage.py shell
```

To create **additional tenants** later (each fully isolated), repeat the
snippet with a new slug/name/admin email. For a second admin *within* an
existing tenant, skip the `Tenant.objects.create(...)` line and reuse that
tenant's `id` in `tenant_context(...)`.

There is also a demo-only seed (`manage.py seed_t0_6`) that creates two
throwaway tenants with a known dev password — **local/CI only, never in
production**.

## Rebuilding & restarting after changes

The `web`/`worker`/`beat` image **bakes in `backend/` source at build time
(no bind mount)**. After any backend change you must rebuild before the change
takes effect — otherwise you silently run stale code.

- **Backend code changed** (anything under `backend/`):
  ```bash
  docker compose build web worker beat migrate
  docker compose up -d
  ```
- **Frontend code changed** (the bundle is baked into the `nginx` image):
  ```bash
  docker compose build nginx
  docker compose up -d nginx
  ```
- **`.env` changed** — restart to pick up new env vars (compose does not
  hot-reload `env_file:`):
  ```bash
  docker compose up -d
  ```
- **New migration added** — just `docker compose up -d`; the `migrate` one-off
  re-runs automatically on every `up` (idempotent).
- **Pulled new code from git** — the always-correct sequence is a full
  rebuild:
  ```bash
  docker compose build
  docker compose up -d
  ```

## Stopping / disabling the stack

```bash
docker compose stop   # stop containers, keep them + volumes for a fast restart
docker compose down   # stop AND remove containers/network; named volumes
                      # (pgdata, redis-data, media, static) are preserved
                      # unless you add -v to also wipe them
```

To disable auto-start on boot, use `docker compose stop`, not `down`: Docker's
`restart: unless-stopped` brings a `down` stack back on the next `up`, whereas
a stopped-but-present stack stays stopped across a Docker daemon restart.
There is no separate "disable" flag — `stop` is the disable.

## Running tests

The image bakes in `backend/` source at build time — **rebuild before
testing** after any backend change. Tests also need the DB-**owner** role (not
the `cortex_app` runtime role) because they `CREATE DATABASE`:

```bash
docker compose build web
docker compose up -d postgres redis
docker compose run --rm --no-deps -u root \
  -e DJANGO_SETTINGS_MODULE=config.settings.test \
  -e DATABASE_URL="$(grep '^DATABASE_URL=' .env | cut -d= -f2-)" \
  web sh -c "pip install -r requirements/dev.txt -q && pytest -q"
```

`pytest`/`ruff`/`black`/`mypy` are not in the base image — install them per
invocation via `requirements/dev.txt` (as `-u root`; the `app` user has no
shell). The test settings module sets `CELERY_TASK_ALWAYS_EAGER` and other
flags `config.settings.dev` doesn't.

Frontend:

```bash
cd frontend && npm run typecheck && npm run lint && npm run build
```

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
├── tests/load/           # Locust load test + results (50k-asset perf gate)
├── .env.example          # every documented env key, placeholder values only
└── CLAUDE.md             # working agreement for AI coding agents
```

**Backend apps** (`backend/apps/`), one per domain area: `tenancy` (tenant
core + RLS helpers), `accounts` (users), `rbac` (roles/permissions/
memberships), `catalog` (categories/locations/custom fields), `projects`,
`assets` (registry, search, attachments, QR resolver), `stock` (consumables
ledger), `reservations` (booking + checkout), `labels` (QR label PDFs),
`jobs` (generic async-job polling, backs labels/imports), `imports` (bulk
CSV/Excel import + CSV export), `notifications` (email), `audit`,
`dashboard`, `common` (shared test factories/utilities).

## Documentation map

Read in this order before making changes: [`overview.md`](overview.md),
[`architecture.md`](architecture.md), [`data-model.md`](data-model.md),
[`rbac.md`](rbac.md), [`api-and-ui.md`](api-and-ui.md),
[`features.md`](features.md), [`deployment.md`](deployment.md),
[`roadmap.md`](roadmap.md), [`risks.md`](risks.md). The (complete) build plan
lives in [`tasks/`](tasks/) — one file per milestone (M0–M6), each with
per-task exit criteria; useful as a map of what was built and why. Post-MVP
feature milestones live alongside them (e.g.
[`tasks/M7-project-grants.md`](tasks/M7-project-grants.md)).

## Known, accepted gaps (MVP)

- **On-device mobile verification** (camera QR scan, photo capture) hasn't
  been exercised on a real phone yet — it needs the live HTTPS Tunnel deploy
  (secure context) to test. Automated coverage (resolver, QR round-trip
  decode, PWA installability) is in place; see
  [`tasks/M4-mobile-scan-labels.md`](tasks/M4-mobile-scan-labels.md).
- **Asset list/search performance at 50k+ rows** misses its p95 latency
  target under load. Root cause: PostgreSQL's Row-Level Security enforces the
  tenant filter as a security barrier, and the search operators aren't marked
  `LEAKPROOF`, so the planner can't use the search indexes below that barrier.
  The fix would override a deliberate upstream PostgreSQL security decision
  cluster-wide — deliberately not applied; see `tests/load/README.md` for the
  full analysis and `backend/apps/assets/tests/test_perf_rls_search_plan.py`
  for the regression tripwire.
- A few open product questions remain in [`risks.md`](risks.md) §3 (exact
  deploy hostname/sender domain, Avery label stock, a representative import
  spreadsheet) — the code uses documented, flagged defaults until answered.
