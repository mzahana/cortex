# M0 — Foundations (scaffold)

**Goal:** the stack boots, login works, tenant isolation is enforced and tested.
**Dep:** none. **Effort:** M.
**Milestone exit:** login works; tenant isolation enforced in a test; CI green.
Refs: `docs/architecture.md`, `docs/deployment.md`, `docs/data-model.md`, `docs/rbac.md`.

---

### T0.1 · → devops-engineer · deps: none
**Do:** Create the repo layout — `backend/` (Django+DRF), `frontend/` (React+TS+Vite
PWA), `docker/` (compose, nginx, cloudflared), keeping `docs/`. Add root README, root
`.gitignore`, `.env.example` with every documented key (SECRET_KEY, DATABASE_URL,
REDIS_URL, BREVO_API_KEY, TUNNEL_TOKEN, ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS,
MEDIA_STORAGE_*), and `git init`.
**Files:** repo root, `.env.example`.
**Exit:** tree matches `docs/roadmap.md` M0; no secrets committed.

### T0.2 · → devops-engineer · deps: T0.1
**Do:** Author `docker-compose.yml` with all 7 services (postgres:16-alpine,
redis:7-alpine, web, worker, beat, nginx:alpine, cloudflared) with the `mem_limit`
values and `restart: unless-stopped` from `docs/deployment.md`. web+worker share one
Dockerfile (different commands). Redis capped `maxmemory 256mb allkeys-lru`. Media
volume shared into web/worker/nginx. Local dev comes up via `docker compose up`.
**Files:** `docker-compose.yml`, `docker/Dockerfile`, `docker/nginx/*.conf`,
`docker/redis.conf`.
**Exit:** `docker compose up` starts all services healthy locally; total mem_limits ≈ 3.25 GB.

### T0.3 · → backend-engineer · deps: T0.2
**Do:** Base Django 5 project, 12-factor settings via env (django-environ), Postgres +
Redis (cache + sessions + broker) + Celery + beat wired. Argon2 hasher; DRF installed;
`DEBUG` off by default; SECURE_* headers, CSRF_TRUSTED_ORIGINS, ALLOWED_HOSTS from env.
`/api/v1` router mounted. A trivial Celery task proves the broker path.
**Files:** `backend/config/settings/*.py`, `backend/config/celery.py`, `backend/config/urls.py`.
**Exit:** `manage.py check --deploy` clean; a queued Celery task executes in `worker`.

### T0.4 · → backend-engineer · deps: T0.3
**Do:** Implement Tenant, User, Role, Permission, RolePermission, Membership models per
`docs/data-model.md`. Seed the system roles (Admin/ProjectLead/Member/Viewer) and the
full permission-key set from `docs/rbac.md` via a data migration. Implement the
**central tenant-scoped base manager + DRF middleware/permission** that injects the
tenant from the session and filters every tenant-owned query. Implement the
union-of-memberships permission resolver (`docs/rbac.md` §1).
**Files:** `backend/apps/tenancy/`, `backend/apps/accounts/`, `backend/apps/rbac/`.
**Exit:** effective-permission resolver returns correct results for the 4 roles incl.
project-scoped vs tenant-wide; new users default to Member.

### T0.5 · → db-migration-specialist · deps: T0.4
**Do:** Migrations for the T0.4 models. Enable `btree_gist` and `pg_trgm` extensions.
Add **RLS policies** on every tenant-owned table keyed off the session's current
tenant, as the isolation backstop (R4). Confirm the app filter and RLS agree.
**Files:** `backend/apps/*/migrations/`.
**Exit:** migrate up **and** down succeed on a scratch DB; a psql check confirms RLS
blocks a cross-tenant `SELECT` even with RLS-bypassing app code disabled.

### T0.6 · → backend-engineer · deps: T0.4
**Do:** Auth endpoints: `POST /api/v1/auth/login`, `POST /auth/logout`, `GET /me`
(returns user, memberships, effective permissions). Redis-backed sessions; Secure/
HttpOnly/SameSite cookies; DRF throttling + login backoff on the login route.
**Files:** `backend/apps/accounts/api.py`.
**Exit:** login sets a session cookie; `/me` returns correct permissions; brute-force
throttled.

### T0.7 · → frontend-engineer · deps: T0.2
**Do:** Scaffold the Vite React+TS app with Mantine, routing, and a typed API client
(`/api/v1` base, CSRF on writes, RFC-7807 error handling). Build the Login screen and a
placeholder authenticated shell reading `GET /me`. nginx serves the built bundle.
**Files:** `frontend/src/*`, `frontend/vite.config.ts`.
**Exit:** `npm run build` + `tsc` clean; login round-trips against the backend; a
logged-in user sees their name from `/me`.

### T0.8 · → devops-engineer · deps: T0.3, T0.7
**Do:** CI pipeline: lint (ruff/black, eslint), typecheck (mypy, tsc), test (pytest,
frontend), build images. CI runs migrations up **and** down against a throwaway Postgres.
**Files:** `.github/workflows/ci.yml` (or chosen CI).
**Exit:** pipeline green on a clean checkout.

### T0.9 · → qa-test-engineer · deps: T0.5, T0.6 · **milestone gate**
**Do:** Tests proving the M0 exit: (a) a tenant-A user gets 404/403 on tenant-B objects,
including with a guessed URL and with the app filter bypassed (RLS catches it);
(b) a Member gets a server-side 403 on an admin endpoint; (c) a ProjectLead's scope is
limited to their project. Wire multi-tenant factory_boy factories defaulting to distinct
tenants.
**Exit:** F1 acceptance proven in CI; **code-reviewer** signs off the M0 diff.
