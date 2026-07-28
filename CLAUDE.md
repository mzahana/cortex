# Cortex — Lab Asset & Inventory Management Platform

A mobile-first, multi-tenant platform to track a robotics lab's physical assets
(compute/GPUs, edge devices, drones, components, tools, instruments): what we own,
where it is, who has it, when it's due, and when consumables run low. Self-hosted on a
Synology DS220+ via docker-compose, exposed over a Cloudflare Tunnel.

## Start here
1. **Design docs are the source of truth** — read `docs/` before coding:
   `overview.md`, `architecture.md`, `data-model.md`, `rbac.md`, `api-and-ui.md`,
   `features.md`, `deployment.md`, `deployment-runbook.md`, `roadmap.md`, `risks.md`.
2. **Status: the MVP is complete** — all milestones M0–M6 are built, tested, and
   signed off (see `CHANGELOG.md` for what shipped in each, and the README's "Known,
   accepted gaps" section for the two open items: on-device mobile verification
   pending a live deploy, and asset list/search perf at 50k+ rows pending an RLS/
   perf tradeoff decision). `docs/tasks/` (a README plus one file per milestone) is
   now a historical build plan — read it to understand what a feature is *for* and
   how it was verified, not as a queue of open work. New work on this repo is
   feature requests, bug fixes, or the deploy/product decisions still open in
   `docs/risks.md` §3 — not milestone tasks.
3. **Before touching a feature, find how it was built and verified**: check
   `docs/tasks/M<N>-*.md` for the task that introduced it (exit criterion + owning
   agent), the matching section of `CHANGELOG.md` for what shipped and any bugs
   caught by review, and the app's own `tests/` for the acceptance tests that prove
   it — these are usually a faster, more accurate way to understand intended
   behavior than reading implementation code cold.

## Stack (locked — see architecture.md)
Django 5 + DRF + PostgreSQL 16 · React + TypeScript + Vite PWA + Mantine · Celery +
Redis (cache + sessions + broker) · nginx · cloudflared · WeasyPrint + segno (label
PDFs) · @zxing/browser (QR scan) · django-storages (media) · Brevo (email, behind an
`EmailProvider` interface). Backend in `backend/apps/` (one Django app per domain area —
`tenancy`/`accounts`/`rbac` are the multi-tenant core; `jobs` is a generic async-job
poller reused by `labels` and `imports`), frontend in `frontend/`, infra in `docker/`.

## Non-negotiable invariants (apply to every change)
- **Tenant isolation first, centrally.** Every tenant-owned query goes through the
  tenant-scoped base manager; the tenant is inferred from the session, **never** from
  the client. Postgres RLS is the backstop on every tenant table. A missed filter is
  the R4 data-leak bug — the highest-stakes mistake in this codebase.
- **RBAC server-side on every endpoint**, after tenant isolation, using the exact
  permission keys and union-of-memberships scope rule in `rbac.md`. UI gating is never
  the security boundary.
- **Audit every mutating action** in `rbac.md` §5 with an immutable before/after entry.
- **Slow work runs in Celery**; requests never block. Email only via `EmailProvider`.
- **12-factor**: all config via env; no local state in `web`; no secrets in image/git.
- **Lists are server-side** paginated/filtered; frontend never loads "all assets".

## Subagents (`.claude/agents/`) — route each task to its owner
`backend-engineer` (Django/DRF, RBAC, Celery) · `db-migration-specialist` (migrations,
RLS, indexes, exclusion/immutability constraints) · `frontend-engineer` (React/TS PWA
screens) · `pwa-scan-specialist` (service worker, camera QR/photo, label PDFs) ·
`devops-engineer` (compose, nginx, cloudflared, CI, deploy) · `qa-test-engineer`
(tests + acceptance) · `code-reviewer` (read-only gate).

## Skills (`.claude/skills/`)
`add-endpoint`, `add-migration`, `add-screen` — golden-path checklists; invoke the
matching one when doing that kind of work.

## Workflow per change
**implement (owning agent) → qa-test-engineer proves it works → code-reviewer gates the
diff → fix findings.** Nothing is done until it's *verified*, not just written — this
applied to every milestone task and applies equally to post-MVP fixes/features. When work
hits an open question from `risks.md` §3 (NAS RAM, hostname, sender domain, spreadsheet
sample, Avery stock, etc.), use the documented default and flag the assumption, or ask
the user.

## Conventions
API under `/api/v1`, RFC-7807 errors. Argon2 hashing; Secure/HttpOnly/SameSite cookies;
CSRF on writes; DRF throttling on auth. Kill N+1s with `select_related`/`prefetch_related`
and assert query budgets. Match the surrounding code's style. Commit/push only when the
user asks.

## Debugging & operational gotchas
Lessons from building this repo that aren't obvious from reading the code cold:

- **The `web`/`worker`/`beat` image bakes in `backend/` source at build time (no bind
  mount).** After ANY backend code change, `docker compose build web` before trusting
  `docker compose run web pytest` — otherwise you silently run stale code. This has
  caused real false-negative/false-positive test runs more than once.
- **`docker compose restart` does NOT pick up a newly built image for the long-running
  `web`/`worker`/`beat` services — only `docker compose up -d` (which recreates the
  container) does.** `restart` just restarts the existing container's process on its
  existing image layer. After `docker compose build web`, use `docker compose up -d web
  worker beat` to actually run the new code/migrations, not `restart`. Caught live: a
  migration that was already applied to the DB (via a fresh one-off `migrate`/`run`
  container) still wasn't visible to the actively-serving `web` container after
  `restart`, because `web` itself was still running the old pre-migration image —
  `django_migrations` looked applied from one angle and not from the other,
  which is confusing to debug if you don't know to check `up -d` vs `restart` first.
- **`pytest`/`ruff`/`black`/`mypy` aren't in the base image** — install them per
  invocation: `pip install -r requirements/dev.txt` (run as `-u root`, since the `app`
  user has no shell for `su`). Test settings need
  `-e DJANGO_SETTINGS_MODULE=config.settings.test` (sets `CELERY_TASK_ALWAYS_EAGER`,
  among other things `config.settings.dev` doesn't).
- **`pytest` needs the DB-owner role, not the runtime role.** `web`/`worker`/`beat` run
  as the non-superuser `cortex_app` role (so RLS actually fires) and can't `CREATE
  DATABASE`. Override `DATABASE_URL` to the owner credentials from `.env` when invoking
  pytest.
- **RLS means "as the app" and "as the owner" are different worlds.** A `manage.py
  shell` or raw Django ORM call outside `tenant_context(...)` will be rejected by Row-
  Level Security (`InsufficientPrivilege`) if run over the `cortex_app` connection, or
  will silently bypass tenant scoping entirely if run over the owner connection. A perf/
  `EXPLAIN` test that runs on the owner connection can pass while the exact same query
  is measurably slower (or uses a different query plan) for real traffic — see
  `backend/apps/assets/tests/test_perf_rls_search_plan.py` for why, and always drive an
  `EXPLAIN`/security-relevant test through the real `cortex_app` role, not the owner.
- **RLS + search operators is a real, load-bearing tradeoff, not a bug to "fix" by
  reflex.** Postgres won't push `@@`/`%` (full-text/trigram) below the RLS tenant
  predicate because those operators aren't `LEAKPROOF` — marking them leakproof would
  restore fast search but overrides a deliberate upstream Postgres security decision,
  cluster-wide, for every RLS table. Don't "fix" this without going back to the user;
  see `tests/load/README.md` for the full analysis.
- **Alpine/musl wheel gaps bite silently.** This image is `python:3.12-alpine`.
  `opencv-python-headless` (and other packages with heavy C/C++ extensions) may have no
  musllinux wheel at all — `pip install` either fails loudly (good) or, worse, a package
  that installs fine on a glibc CI runner can be completely broken/absent in this actual
  image. Before adding a new Python dependency, sanity-check it has a musllinux wheel
  (`pip download --no-deps --only-binary=:all: <pkg>` against a throwaway
  `python:3.12-alpine` container) or expect to debug this the hard way.
- **`.env` is gitignored and NOT tracked** — there is no git history to recover it from
  if it's ever deleted or clobbered. It has genuinely happened that a subagent deleted a
  local `.env` while testing something unrelated and then misreported having left it
  alone; the deletion was only caught by noticing the file's own reconstructed content
  didn't match expectations. Treat `.env` as irreplaceable local state: never delete or
  overwrite it without confirming with the user first, and independently verify any
  subagent's claim about having left it alone rather than trusting the claim.
- **`CSRF_TRUSTED_ORIGINS=http://localhost` (the default in `.env.example`) doesn't
  match `http://localhost:<port>`.** If you publish nginx on a non-default port for
  local browser testing, add the port-specific origin to `.env` temporarily and revert
  it afterward (diff against `.env.example`/a backup to confirm you reverted cleanly).
