# LMS — Lab Asset & Inventory Management Platform

A mobile-first, multi-tenant platform to track a robotics lab's physical assets
(compute/GPUs, edge devices, drones, components, tools, instruments): what we own,
where it is, who has it, when it's due, and when consumables run low.

Self-hosted on a Synology DS220+ via `docker compose`, exposed only through a
Cloudflare Tunnel (no inbound router ports). See `docs/` for the full design —
start with `docs/overview.md` and `docs/architecture.md`.

## Stack

- **Backend:** Django 5 + Django REST Framework, PostgreSQL 16 (RLS for tenant
  isolation), Redis (cache + sessions + Celery broker), Celery + beat, Argon2
  password hashing.
- **Frontend:** React + TypeScript + Vite, built as a PWA (Mantine UI,
  `@zxing/browser` for QR scanning).
- **Docs/labels:** WeasyPrint + segno for QR label PDFs.
- **Email:** Brevo, behind an `EmailProvider` interface.
- **Media:** `django-storages`, filesystem-backed volume for MVP, swappable to
  S3-compatible object storage later via env config only.
- **Infra:** nginx (internal reverse proxy + static/media), cloudflared
  (outbound-only tunnel), all 7 services orchestrated by one `docker-compose.yml`.

## Repository layout

```
.
├── backend/        # Django + DRF project (apps/, config/)
├── frontend/        # React + TS + Vite PWA
├── docker/         # Dockerfile, nginx conf, cloudflared conf, redis.conf
├── docker-compose.yml
├── docs/           # design docs — source of truth, read before coding
│   └── tasks/      # milestone task breakdown (M0–M6)
├── .env.example    # every documented env key, placeholder values only
├── .gitignore
└── CLAUDE.md       # working agreement for AI coding agents on this repo
```

## Getting started (local dev)

1. Copy `.env.example` to `.env` and fill in real values (never commit `.env`).
2. `docker compose up` — brings up postgres, redis, web, worker, beat, nginx,
   cloudflared (cloudflared is a no-op locally without a real `TUNNEL_TOKEN`).
3. Backend: `docker compose exec web python manage.py migrate`.
4. Frontend dev server: `cd frontend && npm install && npm run dev` (or use the
   nginx-served build from the `web` container).

## Documentation map

Read in this order before making changes: `docs/overview.md`,
`docs/architecture.md`, `docs/data-model.md`, `docs/rbac.md`,
`docs/api-and-ui.md`, `docs/features.md`, `docs/deployment.md`,
`docs/roadmap.md`, `docs/risks.md`. The build plan lives in `docs/tasks/`
(one file per milestone, M0–M6).

## Non-negotiable invariants

See `CLAUDE.md` at the repo root for the full list (tenant isolation via a
central scoped manager + Postgres RLS backstop, server-side RBAC on every
endpoint, audit logging on mutations, 12-factor config, no local state, no
secrets in the image/git, server-side pagination).

## Status

Milestone **M0 — Foundations** in progress. See `docs/roadmap.md` and
`docs/tasks/M0-foundations.md` for the current task list and exit criteria.
