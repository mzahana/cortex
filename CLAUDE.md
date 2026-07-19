# LMS — Lab Asset & Inventory Management Platform

A mobile-first, multi-tenant platform to track a robotics lab's physical assets
(compute/GPUs, edge devices, drones, components, tools, instruments): what we own,
where it is, who has it, when it's due, and when consumables run low. Self-hosted on a
Synology DS220+ via docker-compose, exposed over a Cloudflare Tunnel.

## Start here
1. **Design docs are the source of truth** — read `docs/` before coding:
   `overview.md`, `architecture.md`, `data-model.md`, `rbac.md`, `api-and-ui.md`,
   `features.md`, `deployment.md`, `roadmap.md`, `risks.md`.
2. **The build plan** is `docs/tasks/` — a README plus one file per milestone (M0–M6),
   ~50 tasks. Each task has an owning agent, dependencies, and a verifiable Exit
   criterion. **Work tasks in dependency order, one at a time.** Current status: see
   the memory note / ask the user which milestone is in progress (begins at **M0**).

## Stack (locked — see architecture.md)
Django 5 + DRF + PostgreSQL 16 · React + TypeScript + Vite PWA + Mantine · Celery +
Redis (cache + sessions + broker) · nginx · cloudflared · WeasyPrint + segno (label
PDFs) · @zxing/browser (QR scan) · django-storages (media) · Brevo (email, behind an
`EmailProvider` interface). Backend in `backend/`, frontend in `frontend/`, infra in
`docker/`.

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

## Workflow per task
**implement (owning agent) → qa-test-engineer proves the Exit criterion →
code-reviewer gates the diff → fix findings.** A task is done only when its Exit
criterion is *verified*, not just written. When a task hits an open question from
`risks.md` §3 (NAS RAM, hostname, sender domain, spreadsheet sample, etc.), use the
documented default and flag the assumption, or ask the user.

## Conventions
API under `/api/v1`, RFC-7807 errors. Argon2 hashing; Secure/HttpOnly/SameSite cookies;
CSRF on writes; DRF throttling on auth. Kill N+1s with `select_related`/`prefetch_related`
and assert query budgets. Match the surrounding code's style. Commit/push only when the
user asks.
