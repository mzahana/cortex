# M6 — Import/export + deploy hardening (MVP complete)

**Goal:** bulk import from the real spreadsheet, filtered export, and a hardened live
deploy on the DS220+ over the Cloudflare Tunnel with tested backups and a passing load
test. End of MVP.
**Dep:** M0–M5. **Effort:** M.
**Milestone exit:** F11 met; live on `lms.<domain>`; backups tested; load test passes
the `docs/architecture.md` §4 targets.
Refs: `docs/features.md` F11, `docs/deployment.md`, `docs/risks.md`.

---

### T6.1 · → backend-engineer · deps: M1
**Do:** CSV/Excel importer: `POST /imports` (upload → **dry-run** validation with a
column-mapping step and a per-row error report), `POST /imports/{id}/commit` (creates
assets incl. custom-field values). Large files run as a **background Celery job**, chunked
to bound worker RAM (R2); poll `GET /jobs/{id}`. Enforce `import.run` (Admin only).
Filtered export `GET /exports/assets.csv` (honors the same filters as the list). Import
must round-trip with export.
**Files:** `backend/apps/imports/`.
**Exit:** F11: a messy spreadsheet imports via mapping + dry-run error report; valid rows
create assets with custom fields; export round-trips.

### T6.2 · → frontend-engineer · deps: T6.1
**Do:** Import screen: upload → map columns → dry-run report → commit, with job progress.
Wire the filtered-list "Export CSV" action.
**Files:** `frontend/src/screens/import/*`.
**Exit:** the full import wizard works end-to-end against a sample file; export downloads.

### T6.3 · → devops-engineer · deps: M0–M5
**Do:** Production hardening pass on the stack: finalize `docker compose` prod profile,
gunicorn workers (2–3) + Celery concurrency (2) + beat, nginx security headers (HSTS, CSP,
X-Content-Type-Options), `DEBUG=false`, pinned base-image digests, mem_limits verified
against the 6 GB budget (and the 2 GB fallback documented in `docs/deployment.md` §8).
**Files:** `docker-compose.prod.yml`, `docker/nginx/*`, hardening notes.
**Exit:** `docs/deployment.md` §7 checklist satisfied; footprint within budget.

### T6.4 · → devops-engineer · deps: T6.3
**Do:** Cloudflare Tunnel + DNS: cloudflared with `TUNNEL_TOKEN`, public hostname
`lms.<domain>` → `http://nginx:80`, SSL Full(strict), HSTS, Always-HTTPS, Bot Fight, and a
rate-limit rule on `/api/v1/auth/login`. Add SPF/DKIM/DMARC records for the sender domain
(needed for F9 deliverability). Produce the **Synology Container Manager runbook**
(shared folders, env-file path, migrate + createsuperuser/seed, auto-restart).
**Files:** `docker/cloudflared/*`, `docs/deployment-runbook.md`.
**Exit:** app reachable at `https://lms.<domain>`; camera scan works on a phone (secure
context); mail passes SPF/DKIM/DMARC. **You cannot touch the physical NAS — hand the
operator exact steps.**

### T6.5 · → devops-engineer · deps: T6.4
**Do:** Backups: nightly `pg_dump` (DSM Task Scheduler) with daily+weekly rotation; media
folder into Hyper Backup (offsite/USB); encrypted `.env`/compose copy. Then run and
document a **restore drill** (R7): restore dump + media into a fresh stack and verify.
**Files:** `docs/deployment-runbook.md` (backup + restore sections), backup scripts.
**Exit:** backups scheduled; **restore drill executed and documented** — data comes back.

### T6.6 · → qa-test-engineer · deps: T6.3 · **perf gate**
**Do:** Load test against the deployed (or prod-profile) stack verifying the
`docs/architecture.md` §4 p95 targets at 50k assets / ~30 concurrent (list < 300 ms,
search < 500 ms, detail < 250 ms, dashboard < 800 ms cached, checkout write < 300 ms).
Dry-run this against M1 early to catch index/query issues (per `docs/roadmap.md`).
**Files:** `tests/load/*`.
**Exit:** targets met (or documented gaps with the tuning applied).

### T6.7 · → code-reviewer · deps: all · **MVP gate**
**Do:** Final review across the MVP surface: tenant isolation + RLS on every table, RBAC
on every endpoint, audit completeness, no secrets in image/git, deploy hardening. Confirm
F1–F11 acceptance is proven, not just claimed.
**Exit:** MVP signed off — deployable, seeded from the spreadsheet, usable in the workshop.

> **Open questions:** Q8 (representative spreadsheet → importer mapping + validation),
> Q3 (exact hostname + sender/email domain), Q1 (confirm NAS RAM: 2 GB vs 6 GB → sizing),
> Q2 (uptime expectations → whether to bring the cloud tier forward). Q8 and Q3 block
> parts of this milestone; the rest use documented defaults with flagged assumptions.
