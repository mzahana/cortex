# Cortex Implementation Tasks

Executable, milestone-sliced engineering tasks derived from the design docs in
`docs/`. Each task is a self-contained unit you can hand to a Claude Code subagent.

## How this maps to the subagents

Subagents live in `.claude/agents/`. They are **stable specialist roles**; these task
files are the **work** you route to them. Every task is tagged with its owning agent:

| Agent | Owns | Model |
|---|---|---|
| `backend-engineer` | Django/DRF models, serializers, viewsets, RBAC enforcement, tenant scoping, business logic, Celery tasks, EmailProvider | sonnet |
| `db-migration-specialist` | Migrations, RLS, indexes, GiST exclusion, tsvector/pg_trgm, audit-immutability & stock triggers | opus |
| `frontend-engineer` | React/TS Vite PWA screens, Mantine, API client, dynamic forms | sonnet |
| `pwa-scan-specialist` | Service worker/manifest, camera QR scan, photo capture, label PDF generation | sonnet |
| `devops-engineer` | docker-compose, app image, nginx, cloudflared, CI, secrets, deploy, backups | sonnet |
| `qa-test-engineer` | pytest, tenant/RBAC/constraint tests, 10k-asset perf seed, acceptance verification | sonnet |
| `code-reviewer` | Read-only review of every diff against invariants (esp. R4 tenant leak) | opus |

Model choices favor tokens: Opus is reserved for the two areas where a subtle mistake
is expensive and hard to catch (DB-level correctness and review). Everything else runs
on Sonnet. For pure-boilerplate reruns of an already-specified task, Haiku is a fine
override.

## How to run a task with a subagent

Point the agent at both the task and the design docs, e.g.:

> Use the **backend-engineer** subagent. Read `docs/tasks/M1-asset-registry.md` task
> **T1.4** and the referenced design docs, then implement it. Report acceptance status.

Recommended loop per task: **implement → `qa-test-engineer` proves acceptance →
`code-reviewer` gates the diff → fix findings**. Do not consider a task done until its
exit criteria are verified, not just written.

## Sequencing (from `docs/roadmap.md`)

Critical path: **M0 → M1 → (M2 ∥ M3) → M5 → M6**. M4 (scan/labels) runs in parallel
after M1. Frontend and backend within a milestone run in parallel against the fixed
API contract in `docs/api-and-ui.md`.

## Task format

Each task: `id · → owning-agent · deps` then **Do**, **Files**, and **Exit** (the
verifiable acceptance criterion). Exit criteria trace back to `docs/features.md` (F#)
and `docs/roadmap.md` milestone exits.

## Open questions gate

`docs/risks.md` §3 lists 12 open questions. Several block specific tasks (e.g. Q7
custom-field priorities → M1 seed; Q8 spreadsheet sample → M6 importer; Q9 Avery stock
→ M4 label templates; Q3 hostname/email domain → M6 deploy). Tasks note where an answer
is required; use sensible documented defaults if unanswered and flag the assumption.
