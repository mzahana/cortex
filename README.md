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

**Jump to:** [What it does](#what-it-does) · [Stack](#stack) ·
[Repository layout](#repository-layout) ·
[Fresh install — local dev](#fresh-install--local-dev) ·
[Deploying to production](#deploying-to-production-nas--cloudflare-tunnel) ·
[Documentation map](#documentation-map)

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

## Fresh install — local dev

Follow these steps **in order** for a brand-new checkout. Every step is
required unless marked optional; skipping the frontend build (step 3) is
the most common reason `http://localhost` shows a blank/404 page.

**0. Prerequisites:** Docker + Docker Compose v2, Node.js 20+ (for the
frontend build only — not needed inside the containers), git.

**1. Clone and configure secrets/config.** All config is env-based (12-factor,
see `CLAUDE.md`) — nothing else needs editing for a local dev run:

```bash
git clone <this-repo-url> cortex && cd cortex
cp .env.example .env
```

Open `.env` and fill in every `changeme-*` placeholder. For local dev the
defaults for `ALLOWED_HOSTS`/`CSRF_TRUSTED_ORIGINS`/`DJANGO_SETTINGS_MODULE`
etc. in `.env.example` are fine as-is except:
- `SECRET_KEY` — generate one: `python3 -c "import secrets; print(secrets.token_urlsafe(50))"`.
- `POSTGRES_PASSWORD` / `DATABASE_URL`, and `APP_DB_PASSWORD` /
  `APP_DATABASE_URL` — pick real (even if only locally-real) passwords; keep
  each password consistent between the two vars that reference it.
- `BREVO_API_KEY` / `TUNNEL_TOKEN` — can stay as placeholders for local dev;
  email sends will fail (logged, not fatal) and `cloudflared` will just idle
  without a real tunnel token — neither blocks the rest of the stack.
See `.env.example`'s inline comments for what every variable does and which
are required vs. optional; `docs/deployment.md` explains the reasoning
behind the two separate DB roles (`cortex` owner vs. `cortex_app` RLS-subject
runtime role) and other non-obvious choices.

**2. Build the containers.** One shared image serves `web`/`worker`/`beat`/
`migrate` (`docker/Dockerfile`); `postgres`/`redis`/`nginx`/`cloudflared` use
pinned upstream images and don't need building:

```bash
docker compose build
```

**3. Build the frontend PWA bundle.** `nginx` serves static files from
`frontend/dist/`, which is bind-mounted but **not built automatically** —
build it once before first bring-up, and again after any frontend change
you want reflected in the containerized app (the hot-reload dev server in
step 5 is a separate, faster path for active frontend development):

```bash
cd frontend && npm install && npm run build && cd ..
```

**4. Bring the stack up.**

```bash
docker compose up -d
```

This starts all 7 services: `postgres`, `redis`, `migrate` (a one-off that
runs `manage.py migrate --noinput` and exits — no manual migration step
needed), then `web`, `worker`, `beat`, `nginx` (`cloudflared` will restart-
loop harmlessly without a real `TUNNEL_TOKEN`, per step 1). Check everything
came up healthy:

```bash
docker compose ps      # all services should show "Up" (healthy) after ~30-60s
docker compose logs -f web   # tail logs if something looks wrong
```

The app is now reachable at `http://localhost` via nginx. nginx publishes no
host port by default (it's designed to sit behind the Cloudflare Tunnel in
production — see "Deploying to production" below); for local browser access,
add a port mapping via a `docker-compose.override.yml` (compose loads this
file automatically alongside `docker-compose.yml`, no extra flag needed) —
example already provided at the repo root:

```yaml
services:
  nginx:
    ports:
      - "8080:80"
```

then browse `http://localhost:8080`.

**5. (Optional) Frontend dev server with hot reload**, for active frontend
work — proxies `/api` to the backend so you don't need to rebuild `dist/` on
every change:

```bash
cd frontend && npm install && npm run dev
```

**6. Create your first tenant + admin user.** There's no signup flow by
design (multi-tenant, invite-only) — create the first tenant and admin from
a Django shell. Full copy-pasteable snippet:
[`docs/deployment-runbook.md`](docs/deployment-runbook.md) §3c (must run
inside `tenant_context(...)`, since the app connects as a non-superuser
RLS-subject role — a naive `create_superuser()` call will be rejected by Row-
Level Security). In short:

```bash
docker compose exec web python manage.py shell
```

then log in at `http://localhost` (or `:8080` with the override above) with
the tenant slug, email, and password you set.

### Stopping / disabling the stack

```bash
docker compose stop        # stop all containers, keep them (and volumes) for a fast restart
docker compose down        # stop AND remove containers/network (data in named volumes — pgdata,
                            # redis-data, media, static — is preserved; add -v to also wipe volumes)
```

To disable auto-start on boot (Docker's own `restart: unless-stopped` will
otherwise bring everything back after a host/Docker restart), stop the stack
with `docker compose stop`, not `down` — a stopped-but-present stack stays
stopped across a Docker daemon restart, while `down` only removes it until
the next `docker compose up`. There is no separate "disable" flag; `stop` is
the disable.

### Rebuilding & restarting after changes

- **Backend code changed** (anything under `backend/`): the image bakes in
  source at build time (no bind mount) — rebuild before restarting, or you
  silently run stale code:
  ```bash
  docker compose build web worker beat migrate
  docker compose up -d
  ```
- **Frontend code changed**: rebuild the static bundle nginx serves (no
  container rebuild needed, it's a bind mount):
  ```bash
  cd frontend && npm run build && cd ..
  docker compose restart nginx
  ```
- **`.env` changed**: restart the affected services to pick up new env vars
  (compose does not hot-reload `env_file:`):
  ```bash
  docker compose up -d     # recreates any service whose config/env changed
  ```
- **New migration added**: just `docker compose up -d` — the `migrate`
  one-off re-runs automatically on every `up` (it's idempotent; already-
  applied migrations are a no-op) before `web`/`worker`/`beat` start.
- **Pulled new code from git** (dependency or model changes are the common
  case): rebuild, then bring back up — the safe, always-correct sequence is
  simply steps 2-4 of the fresh-install flow above, repeated:
  ```bash
  docker compose build
  cd frontend && npm install && npm run build && cd ..
  docker compose up -d
  ```

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

## Deploying to production (NAS + Cloudflare Tunnel)

Cortex is designed to self-host on a small NAS (reference target: Synology
DS220+) behind a Cloudflare Tunnel (no inbound ports opened, TLS terminated
at the edge). The steps mirror the local fresh install above, plus one-time
Cloudflare/Brevo setup and a production compose overlay:

1. Read [`docs/deployment.md`](docs/deployment.md) for the design-level
   overview (topology, memory budget, hardening checklist) — read this
   first so the runbook's steps make sense.
2. Follow [`docs/deployment-runbook.md`](docs/deployment-runbook.md) for the
   exact, copy-pasteable steps, **in this order**: Cloudflare Tunnel + DNS
   setup (§1), Brevo sender domain SPF/DKIM/DMARC (§2), NAS shared-folder
   layout and `.env` (§3a-3b), then first bring-up (§3c):
   ```bash
   # from the NAS project directory, .env filled in and frontend/dist built:
   docker compose -f docker-compose.yml -f docker-compose.prod.yml build
   docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
   docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
   ```
   `docker-compose.prod.yml` is an **overlay**, not a replacement — always
   pass both `-f` flags together (every command in this section and the
   runbook does). It forces `DJANGO_SETTINGS_MODULE=config.settings.prod`
   as defense-in-depth and makes the gunicorn worker count explicit/tunable
   via `GUNICORN_WORKERS`; everything else (mem limits, restart policy,
   healthchecks, volumes) stays the base file's values.
3. Create the first tenant + admin — runbook §3c (same shell snippet as
   local dev, run against the prod-overlay stack).
4. Run through the runbook's §4 verification checklist (HTTPS reachable,
   security headers, login, edge rate-limit, camera scan on a real phone,
   SPF/DKIM/DMARC pass, reboot survival).
5. Set up backups: `docker/backup/backup.sh` (nightly `pg_dump`,
   daily+weekly rotation) — wire it into DSM Task Scheduler per the
   runbook's §5. **Test your restore before you need it** — §5d documents a
   real, verified restore drill to follow.

**Stopping, disabling, rebuilding, and restarting in production** work
exactly as in the "Stopping / disabling the stack" and "Rebuilding &
restarting after changes" sections above — just add `-f docker-compose.yml
-f docker-compose.prod.yml` to every `docker compose` command. For example,
to stop the whole production stack (e.g. for maintenance) without losing
data:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml stop
```

and to bring it back:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

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
