# API Surface & UI Screens

## 1. API conventions

- REST/JSON under `/api/v1`, DRF. Tenant inferred from the authenticated session —
  **never** passed by the client. Every list endpoint is **paginated**
  (`?page`, `?page_size`, cursor option for large sets) and supports
  `?search=`, `?ordering=`, and field filters.
- Auth: session cookie (web PWA). Optional JWT for future device clients.
- Errors: RFC-7807-style problem+json. Writes are idempotent where sensible
  (e.g. check-in) and audited.

## 2. Key endpoints (representative, not exhaustive)

### Auth & identity
| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/auth/csrf` | Plant the CSRF cookie before login (see below) |
| POST | `/api/v1/auth/login` | Session login |
| POST | `/api/v1/auth/logout` | Logout |
| GET | `/api/v1/me` | Current user, memberships, effective permissions |
| GET/POST/PATCH | `/api/v1/users` `/{id}` | User admin (scoped) |
| GET/POST | `/api/v1/memberships` | Assign role/scope |
| GET | `/api/v1/roles`, `/api/v1/permissions` | RBAC reference |

**Contract frozen at T0.6 (code-review approved — build against this):**

- `GET /api/v1/auth/csrf` — unauthenticated. No body. Sets the
  `lms_csrftoken` cookie (JS-readable) via Django's `ensure_csrf_cookie`; the
  SPA calls this once before its first write so it has a token to echo back
  as the `X-CSRFToken` header on `POST /api/v1/auth/login` (which enforces
  CSRF even though no session exists yet — logout and every other write rely
  on the session-authenticated CSRF check instead). Response:
  `{"detail": "CSRF cookie set."}`.

- `POST /api/v1/auth/login` — request body:
  ```json
  { "tenant": "<tenant-slug>", "email": "user@example.com", "password": "..." }
  ```
  `tenant` is the `Tenant.slug` (required — `User.email` is only unique
  *per tenant*, so the tenant must be disambiguated by the client at login;
  there is no session yet to infer it from). On success (`200`), the
  response body is the same shape as `GET /api/v1/me` (below) and a session
  cookie (`lms_sessionid`, `HttpOnly`, `Secure` in prod, `SameSite=Lax`) is
  set. On failure: a uniform `401` (RFC-7807) — "invalid tenant, email, or
  password" — regardless of which one was actually wrong (never reveals
  whether an email exists, in this tenant or another); a `429` if the
  account is locked (repeated failures) or the endpoint's own rate limit is
  hit.

- `POST /api/v1/auth/logout` — authenticated, CSRF-enforced (session
  already exists). No body. `204 No Content`; clears the session.

- `GET /api/v1/me` — authenticated. Response body:
  ```json
  {
    "id": 1,
    "email": "admin@acme.test",
    "name": "Admin Ada",
    "tenant": { "id": 1, "slug": "acme-robotics", "name": "Acme Robotics Lab" },
    "memberships": [
      {
        "role": "project_lead",
        "role_name": "Project Lead",
        "project_id": 3,
        "project_name": "Project Alpha"
      },
      {
        "role": "member",
        "role_name": "Member",
        "project_id": null,
        "project_name": null
      }
    ],
    "permissions": ["asset.view", "asset.export", "..."],
    "project_permissions": {
      "3": ["asset.view", "asset.create", "..."]
    }
  }
  ```
  - `memberships` — every `Membership` the user holds, tenant-wide
    (`project_id`/`project_name` = `null`) and project-scoped alike.
  - `permissions` — the effective permission set on the tenant's general
    pool (`project=None`): the union of every **tenant-wide** membership's
    role permissions only (docs/rbac.md §1 — project-scoped power never
    reaches the general pool).
  - `project_permissions` — keyed by project id (as a string, JSON object
    keys), one entry per project the user holds a **project-scoped**
    membership on; each value is that project's effective permission set
    (tenant-wide grants UNION'd with that project's scoped grants). Projects
    the user has no scoped membership on are simply absent (their effective
    permission set there is just `permissions` above).
  This is the same shape returned by a successful `POST /api/v1/auth/login`.

### Structure
| Method | Path | Purpose |
|---|---|---|
| GET/POST/PATCH/DELETE | `/api/v1/categories` `/{id}` | Category tree + `requires_approval` etc. |
| GET/POST | `/api/v1/categories/{id}/fields` | Custom field defs |
| CRUD | `/api/v1/locations` | Location tree |
| CRUD | `/api/v1/projects` | Projects |
| GET | `/api/v1/tags` | Tags |

### Assets
| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/assets` | Paginated list; filters: `category,status,location,project,tag,is_consumable`; `search=` (FTS+fuzzy) |
| POST | `/api/v1/assets` | Create (incl. custom field values) |
| GET/PATCH | `/api/v1/assets/{id}` | Detail / edit |
| POST | `/api/v1/assets/{id}/retire` | Retire / mark lost |
| POST | `/api/v1/assets/{id}/attachments` | Upload photo/doc (multipart → volume/object store) |
| GET | `/api/v1/resolve/{qr_token}` | **Scan resolver** → asset id (used by mobile scan flow) |

### Stock
| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/stock` | Consumables incl. low-stock filter |
| POST | `/api/v1/stock/{id}/txn` | Receive / consume / adjust (ledger) |
| GET/POST/PATCH | `/api/v1/reorder-requests` `/{id}` | Reorder workflow |

### Reservations & checkout
| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/reservations` | List / calendar feed (`?from&to`) |
| POST | `/api/v1/reservations` | Create (conflict + limit checked) |
| POST | `/api/v1/reservations/{id}/approve` · `/reject` | Approval (scoped) |
| POST | `/api/v1/reservations/{id}/cancel` | Cancel |
| POST | `/api/v1/checkouts` | Check out (optionally from reservation) |
| POST | `/api/v1/checkouts/{id}/checkin` | Check in with condition |
| POST | `/api/v1/checkouts/{id}/override-return` | Force return |
| GET | `/api/v1/checkouts?open=true&overdue=true` | Open / overdue items |

### Maintenance, labels, import/export, dashboard
| Method | Path | Purpose |
|---|---|---|
| CRUD | `/api/v1/maintenance-plans`, `/api/v1/maintenance` | Schedule / log (Phase 2 UI) |
| POST | `/api/v1/labels/generate` | Body: asset ids + sheet template → enqueues PDF; returns job id |
| GET | `/api/v1/jobs/{id}` | Poll background job (import/label/export) |
| POST | `/api/v1/imports` | Upload spreadsheet → dry-run validation |
| POST | `/api/v1/imports/{id}/commit` | Commit mapped import |
| GET | `/api/v1/exports/assets.csv` | Filtered export |
| GET | `/api/v1/dashboard/summary` | Aggregates (cached) |
| GET | `/api/v1/audit` | Audit log (scoped) |
| GET/PATCH | `/api/v1/notification-prefs` | Per-user prefs |

## 3. UI pages / screens

Mobile-first PWA; every screen usable one-handed on a phone.

| Screen | Purpose |
|---|---|
| **Login** | Auth; "add to home screen" prompt |
| **Dashboard / Home** | Tiles: totals by category, currently-out, overdue, low-stock, upcoming reservations, per-project allocation |
| **Scan** (primary FAB) | Opens camera, scans QR → routes to Asset Detail with quick check-in/out |
| **Asset List** | Server-side search + filters + tags; virtualized list; card + table views |
| **Asset Detail** | Specs (custom fields), photos, status, location, history; actions: reserve, check-out/in, edit, attach photo, generate label, report issue |
| **Asset Create/Edit** | Category-driven dynamic form (custom fields), photo capture, location picker |
| **Stock / Consumables** | Quantities, low-stock highlights, receive/consume, reorder requests |
| **Reservations Calendar** | Month/week/day; create/approve; conflict feedback |
| **My Items** | What I have out, due dates, overdue; quick check-in |
| **Approvals** | Pending reservation/reorder approvals in my scope |
| **Labels** | Select assets → choose Avery sheet template → generate/download PDF |
| **Import** | Upload spreadsheet → map columns → dry-run report → commit |
| **Maintenance** (Phase 2) | Due/overdue plans, log events |
| **Reports** (Phase 2) | Utilization, inventory value, consumption, per-project |
| **Admin: Users & Roles** | Manage users, memberships, project scoping |
| **Admin: Categories & Fields** | Category tree, custom field defs, approval flags |
| **Admin: Locations** | Location tree |
| **Admin: Tenant Settings** | Tenant config, notification defaults, sender domain |
| **Audit Log** | Filterable immutable history |
| **My Notifications** | Per-event email preferences |
| **Profile** | Password, session |
