# M5 — Notifications (Brevo) + audit + dashboard (MVP)

**Goal:** async email via the provider interface, scheduled scans, finalized audit trail,
and the dashboard. Pulls together the domain events emitted in M2/M3.
**Dep:** M2, M3. **Effort:** M.
**Milestone exit:** F8 + F9 + F10 acceptance met.
Refs: `docs/architecture.md` §6, `docs/features.md` F8/F9/F10, `docs/api-and-ui.md`.

---

### T5.1 · → backend-engineer · deps: M0
**Do:** `EmailProvider` protocol (`send_transactional(template_id, to, params, …)`),
`BrevoProvider` (transactional API) and `ConsoleProvider` (dev/tests), selected by env.
Business logic maps **domain events** → templated emails; it never imports Brevo directly.
Every send is a Celery task with retry/backoff. `EmailLog` table (recipient, event,
provider, message-id, status queued|sent|failed|bounced, error). `NotificationPref` per
user × event type gates optional emails (security emails always sent).
**Files:** `backend/apps/notifications/`.
**Exit:** an emitted event enqueues a task and returns immediately; a forced failure
retries and is logged `failed`; a disabled optional event sends nothing.

### T5.2 · → backend-engineer · deps: T5.1, M2, M3
**Do:** Wire the domain events from M2/M3 to templates: reservation confirmation,
approval request, approval decision, overdue reminder, low-stock alert. Celery **beat**
scans: overdue checkouts (using the open-items index) and low-stock (using the low-stock
partial index), each throttled to respect Brevo tier limits.
**Files:** `backend/apps/notifications/tasks.py`, beat schedule.
**Exit:** F9: each event type sends via the queue; beat scans produce reminders without
blocking requests.

### T5.3 · → backend-engineer · deps: M0 · (audit finalize)
**Do:** Finalize the AuditLog: confirm **every** mutating action in `docs/rbac.md` §5
writes an immutable before/after entry (some added in earlier milestones — audit the
gaps here). `GET /api/v1/audit` (scoped: Admin tenant-wide, ProjectLead their project's
assets only).
**Files:** `backend/apps/audit/`.
**Exit:** F8: check-out, stock adjust, reservation approval, and role change each produce
a tamper-evident entry; entries cannot be edited/deleted via the app.

### T5.4 · → db-migration-specialist · deps: T5.3
**Do:** Migrations for EmailLog / NotificationPref. `audit_log (tenant_id, entity_type,
entity_id, created_at)` index. **Append-only trigger** forbidding UPDATE/DELETE on
`audit_log` (DB-level immutability, backing the app guard). RLS on new tables.
**Files:** `backend/apps/audit/migrations/`, `backend/apps/notifications/migrations/`.
**Exit:** up/down clean; a direct UPDATE/DELETE on `audit_log` is rejected by the trigger.

### T5.5 · → backend-engineer · deps: M1, M2, M3
**Do:** `GET /api/v1/dashboard/summary` — aggregates: totals by category, currently-out,
overdue, low-stock, upcoming reservations, per-project allocation. **Cached in Redis**
(short TTL + event-based invalidation); target < 800 ms.
**Files:** `backend/apps/dashboard/`.
**Exit:** tiles reflect live state; cached response within budget.

### T5.6 · → frontend-engineer · deps: T5.5, T5.1
**Do:** Dashboard/Home screen (the six tiles), My Notifications screen (per-event email
prefs), and the Audit Log screen (filterable, read-only).
**Files:** `frontend/src/screens/dashboard/*`, `.../notifications/*`, `.../audit/*`.
**Exit:** dashboard tiles render live data; a user can toggle optional email prefs; audit
log is browsable within scope.

### T5.7 · → qa-test-engineer + code-reviewer · deps: T5.2, T5.3, T5.5 · **milestone gate**
**Do:** F8 (audit immutability + completeness incl. DB-trigger rejection), F9 (enqueue-not-
block, retry→failed, prefs gate, ConsoleProvider in tests), F10 (dashboard reflects state;
list/search < 500 ms on the 10k seed from T1.8) acceptance tests. code-reviewer gates the
diff with focus on audit completeness.
**Exit:** F8 + F9 + F10 met; review signed off.

> **Open questions:** Q6 (Brevo sender identity + account/tier) blocks live send; Q12
> (audit/retired retention). Use ConsoleProvider + documented defaults until answered.
