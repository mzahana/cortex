# Risks, Trade-offs & Open Questions

## 1. Key risks & mitigations

| # | Risk | Impact | Mitigation |
|---|---|---|---|
| R1 | **NAS RAM actually 2 GB, not 6 GB** | Stack too tight; slow dashboards, failed imports | Confirm installed RAM (Open Q1). Design fits 6 GB with headroom; a documented 2 GB fallback exists (`deployment.md §8`). Recommend the cheap 6 GB upgrade. |
| R2 | **Dual-core Celeron under aggregate/dashboard queries at 50k rows** | Slow dashboards, contention with Celery | Indexes + cached dashboard aggregates + chunked imports + bounded Celery concurrency. Tier-2 offloads DB to managed Postgres. |
| R3 | **Residential connection / ISP CGNAT or outages** | App unreachable | Cloudflare Tunnel is unaffected by CGNAT (outbound only). Outage risk is inherent to home hosting → the cloud tier path is the answer if uptime matters. (Open Q2) |
| R4 | **Multi-tenant data leak** (a missed tenant filter) | Serious data exposure | Central queryset scoping + DRF permission layer **and** Postgres RLS backstop + tests that assert cross-tenant 404/403. |
| R5 | **Camera scanning fails on some phones/browsers** | Core mobile flow broken | Requires HTTPS secure context (satisfied by Tunnel). Test on iOS Safari + Android Chrome; provide manual asset-ID entry fallback. |
| R6 | **Email deliverability / Brevo limits** | Alerts land in spam or throttled | SPF/DKIM/DMARC on Cloudflare DNS; respect Brevo tier limits; throttle bulk; EmailLog + retries; monitor bounces. |
| R7 | **Backups exist but restore never tested** | Data loss on failure | Scheduled restore **drill** in M6; documented runbook; offsite copy via Hyper Backup. |
| R8 | **Custom-fields (JSONB) sprawl** hurts query/report clarity | Slow/ugly reports | Typed CustomFieldDef + GIN index; keep filterable specs promoted to indexed columns where heavily used. |
| R9 | **Scope creep / gold-plating** for a lab-scale tool | Delays usable MVP | Strict MVP boundary in `features.md`; deferred list is explicit. |
| R10 | **Single-NAS = single point of failure** (disk, power) | Downtime/data loss | RAID + Hyper Backup; documented tier-2/3 cloud path; keep app stateless so failover is a redeploy. |

## 2. Notable trade-offs (decided, with rationale)

- **Django over Node full-stack** — fewer containers, batteries-included
  RBAC/admin/migrations for a small team, at the cost of a two-language codebase
  (Python + TS). Accepted: the app is data/RBAC-heavy, Django's strength.
- **Shared-schema multi-tenancy over schema/db-per-tenant** — leanest for a NAS and
  tens of tenants; RLS mitigates the isolation risk. Schema-per-tenant is the
  escape hatch if a tenant needs physical isolation.
- **Postgres FTS over a search engine** — one fewer heavy container; sufficient at
  50k rows. Revisit only if search needs outgrow it.
- **Redis triple-duty (cache+sessions+broker)** — one small container vs. three;
  capped memory. Accepted minor coupling.
- **PDF label sheets over label printer** (your choice) — no hardware integration
  now; printer support deferred to Phase 3.
- **App login only over Cloudflare Access** (your choice) — lower member friction;
  Access remains a documented toggle for admin routes.

## 3. Open questions / decisions I need from you

1. **Confirm the DS220+ installed RAM** — 2 GB or upgraded to 6 GB? (Drives sizing;
   see R1.) I recommend 6 GB.
2. **Uptime expectations** — is occasional home-connection/power downtime
   acceptable for the pilot, or should we plan the cloud tier sooner? (R3/R10)
3. **Custom domain & hostname** — what exact hostname should the app live on
   (e.g. `cortex.yourdomain.com`), and is the sender/email domain the same? (Needed
   for Tunnel + SPF/DKIM/DMARC.)
4. **Photo storage** — mounted NAS volume for MVP acceptable, or do you want an
   S3-compatible bucket (e.g. Backblaze B2) from day one? (Cheap to defer.)
5. **Multi-tenant onboarding** — who creates tenants? A platform SuperAdmin (you)
   only, or self-service signup? MVP assumes SuperAdmin-created tenants.
6. **Email sender identity** — sender name/address and reply-to for Brevo; is a
   Brevo account already set up (which tier/limits)?
7. **Custom field priorities** — please confirm the must-have spec fields per
   category (esp. Compute/GPU and Drone electronics) so the seed field defs are
   right on first import.
8. **Existing spreadsheet sample** — can you share one representative export? It
   directly shapes the importer's default column mapping and validation.
9. **Avery label stock** — which specific label sheet product(s)/sizes will you
   print on? Determines the default PDF sheet templates.
10. **Reservation limits** — sensible default caps (max active reservations per
    user, max days ahead, max duration)? Configurable, but need defaults.
11. **Barcode vs QR** — confirm QR-only for MVP (recommended) or do any existing
    assets already carry 1-D barcodes we must read?
12. **Data retention** — any policy for audit log / retired assets retention?
