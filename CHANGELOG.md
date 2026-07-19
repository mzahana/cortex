# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Versions map to milestones in `docs/tasks/`: **M0** → `0.1.0`, and subsequent
milestones bump the minor version until the first production release (`1.0.0`).

## [Unreleased]

_M2 (consumables & stock) next — see `docs/tasks/M2-consumables-stock.md`._

## [0.2.0] - 2026-07-19

Milestone **M1 — Asset registry core**: the heterogeneous asset registry with
custom fields, trees, full-text search, and the admin/list/detail/edit UI.
Meets F2 acceptance; a 10k-row corpus searches in well under 500 ms server-side.

### Added

- **Catalog** — `Category` and `Location` self-referential trees, typed
  `CustomFieldDef`s (text/int/float/bool/date/enum/json with unit, enum options,
  required, order), `Tag`s, and the `Project` model (extended with a lead), all
  tenant-scoped with server-side-paginated CRUD endpoints and
  `category.manage`/`location.manage` enforcement.
- **Assets** — the `Asset` model (public UUID + unguessable `qr_token`, category,
  status lifecycle, condition, holder, JSONB custom field values validated
  against the category's field defs), `Attachment`s (bytes on the media volume,
  only the key in the database, with a size cap and content-type allowlist),
  tag links, and CRUD + retire (hide-but-retain, audited) + attachment upload.
  Asset actions are RBAC-enforced with the project-scoped union-of-memberships
  rule.
- **Asset list & search** — server-side pagination (page and cursor), full-text
  search (weighted `tsvector` maintained by database triggers) with `pg_trgm`
  fuzzy fallback and relevance ranking, whitelisted ordering, and filters by
  category, status, location, project, tag, and consumable flag — scope-aware so
  a project-scoped user sees only their assets, with bounded query counts.
- **Audit log** — an append-only `AuditLog` (pulled forward to satisfy the audit
  invariant for `asset.retire`; database-level immutability lands in M5).
- **Admin & asset UI** — a reusable tree component; admin screens for the
  category/field-definition and location trees; a virtualized, filterable,
  searchable Asset List (card and table views); an Asset Detail screen rendering
  typed custom fields with permission-gated actions; and a category-driven
  dynamic Asset create/edit form.
- **Performance tooling** — a reusable 10k+ asset seed command and a perf test
  suite asserting sub-500 ms paginated list and search at scale.

### Changed

- Database constraint violations (duplicate names, protected deletes) now return
  RFC 7807 4xx responses instead of HTTP 500.
- Django's admin moved from `/admin/` to `/django-admin/` so the single-page
  app owns the `/admin/*` route namespace.

### Security

- Row-Level Security policies added to all nine new tenant-owned tables via the
  shared helper, keeping the application filter and the database backstop in
  lockstep; verified by a runtime test that drives a real request over the
  non-superuser application role.
- Tenant provisioning fixed to seed roles within the new tenant's context so it
  works under Row-Level Security. Attachment uploads reject executable/script
  content types; user-supplied content is escaped throughout the UI.

### Fixed

- Renaming a tag now refreshes the search vectors of every asset carrying it.

## [0.1.0] - 2026-07-19

Milestone **M0 — Foundations**: the stack boots, login works, and multi-tenant
isolation is enforced at two independent layers and proven by tests in CI.

### Added

- **Repository scaffold** — `backend/` (Django + DRF), `frontend/` (React + TS +
  Vite PWA), `docker/` (compose, nginx, cloudflared), design docs in `docs/`, and
  `.env.example` documenting every configuration key.
- **Container stack** — `docker-compose.yml` with all seven services
  (postgres 16, redis 7, web, worker, beat, nginx, cloudflared) under a
  ~3.25 GB memory budget, a shared application image, a one-off owner-role
  `migrate` step, capped Redis (`maxmemory 256mb allkeys-lru`), and nginx serving
  the built PWA behind the Cloudflare Tunnel with security headers.
- **Django project** — 12-factor split settings via `django-environ`, Redis as
  cache + session store + Celery broker/result backend, Celery + beat wiring, and
  the versioned `/api/v1` API surface with RFC 7807 error responses.
- **Multi-tenant core & RBAC** — `Tenant`, custom `User` (email unique per
  tenant), `Role`, `Permission`, `RolePermission`, and `Membership` models; the
  central fail-closed tenant-scoped base manager; the union-of-memberships
  effective-permission resolver (tenant-wide vs. project-scoped); and an
  idempotent seed of the four system roles and full permission-key set.
- **Session authentication** — `POST /api/v1/auth/login`, `POST /api/v1/auth/logout`,
  `GET /api/v1/me` (user, memberships, effective permissions), plus
  `GET /api/v1/auth/csrf`. Redis-backed sessions, Secure/HttpOnly/SameSite
  cookies, CSRF on writes, per-IP request throttling, and failure-based login
  lockout. The auth contract is frozen in `docs/api-and-ui.md`.
- **Frontend** — Vite + React + TypeScript + Mantine scaffold with routing, a
  typed API client (CSRF handling, RFC 7807 parsing), a Login screen, and an
  authenticated shell that reads `GET /me`.
- **Continuous integration** — GitHub Actions pipeline: ruff/black, mypy,
  ESLint, `tsc`, Vite build, pytest against a throwaway Postgres 16, migrations
  applied **up and down**, `manage.py check --deploy` under production settings,
  and an application-image build.
- **Test suite** — multi-tenant `factory_boy` factories defaulting to distinct
  tenants; tenant-isolation, RBAC-scope, and query-budget tests; and a canonical
  Row-Level Security test verified with a negative control.

### Security

- **Tenant isolation is enforced centrally and defence-in-depth.** The tenant is
  derived only from the server-side session, never from client input. Every
  tenant-owned query passes through the fail-closed tenant-scoped manager, and
  **PostgreSQL Row-Level Security** is the backstop: the runtime connects as a
  dedicated non-superuser, `NOBYPASSRLS` role (`cortex_app`) so policies actually
  fire, while migrations run as the owner. A missing tenant context yields zero
  rows rather than leaking across tenants.
- Argon2 password hashing; production `SECURE_*` headers, HSTS, and secure
  cookies configured from the environment; no secrets committed to the repository
  or baked into images.
