# RBAC — Roles & Permissions

## 1. Model

Three concepts (see `data-model.md`):

- **Permission** — an atomic action key (`asset.create`, `stock.adjust`, …).
- **Role** — a named bundle of permissions.
- **Membership** — binds a `User` to a `Role` **within a scope**: either
  **tenant-wide** (`project_id = NULL`) or **project-scoped** (`project_id` set).

A user may hold several memberships (e.g. Member tenant-wide + ProjectLead on
Project X). Effective permission for an action on an asset =
**union of permissions from all memberships whose scope covers that asset**, then
enforced server-side on every endpoint, *after* tenant isolation.

**Scope resolution for an action on an asset:**
1. Tenant isolation first — asset must belong to the user's tenant (also enforced by Postgres RLS).
2. If a tenant-wide membership grants the permission → allowed.
3. Else if the asset has `project_id = P` and the user has a project-scoped
   membership on `P` granting the permission → allowed.
4. Else denied.

General-pool assets (`project_id = NULL`) are governed by **tenant-wide**
permissions only — a ProjectLead's power does **not** extend to the shared pool
unless they also hold a tenant-wide role.

## 2. Default roles

| Role | Intent | Typical scope |
|---|---|---|
| **Admin / Lab Manager** | Full control of the tenant | Tenant-wide |
| **Project Lead** | Manage assets & approve requests for *their* project | Project-scoped |
| **Member** | Browse, reserve/checkout, report issues, request consumables | Tenant-wide (usually) |
| **Viewer / Guest** | Read-only | Tenant-wide |
| *(SuperAdmin)* | Cross-tenant platform operator (you) | Global — outside normal RBAC |

## 3. Permissions matrix

Legend: ✅ allowed · 🟡 scoped (only within the user's project) · ➖ denied.

| Action (permission key) | Admin | Project Lead | Member | Viewer |
|---|:--:|:--:|:--:|:--:|
| View inventory / assets (`asset.view`) | ✅ | ✅ | ✅ | ✅ |
| Search / filter / export list (`asset.export`) | ✅ | 🟡 | ✅ | ➖ |
| Add asset (`asset.create`) | ✅ | 🟡 | ➖ | ➖ |
| Edit asset (`asset.edit`) | ✅ | 🟡 | ➖ | ➖ |
| Retire / mark lost (`asset.retire`) | ✅ | 🟡 | ➖ | ➖ |
| Upload photo/attachment (`asset.attach`) | ✅ | 🟡 | ✅¹ | ➖ |
| Manage categories & custom fields (`category.manage`) | ✅ | ➖ | ➖ | ➖ |
| Manage locations (`location.manage`) | ✅ | ➖ | ➖ | ➖ |
| Adjust stock / receive (`stock.adjust`) | ✅ | 🟡 | ➖ | ➖ |
| Consume stock (`stock.consume`) | ✅ | ✅ | ✅ | ➖ |
| Request reorder (`reorder.request`) | ✅ | ✅ | ✅ | ➖ |
| Approve reorder (`reorder.approve`) | ✅ | 🟡 | ➖ | ➖ |
| Create reservation (`reservation.create`) | ✅ | ✅ | ✅ | ➖ |
| Approve/reject reservation (`reservation.approve`) | ✅ | 🟡 | ➖ | ➖ |
| Check out / check in (`checkout.manage`) | ✅ | ✅ | ✅² | ➖ |
| Force-return / override checkout (`checkout.override`) | ✅ | 🟡 | ➖ | ➖ |
| Report an issue (`issue.report`) | ✅ | ✅ | ✅ | ➖ |
| Schedule/log maintenance (`maintenance.manage`) | ✅ | 🟡 | ➖ | ➖ |
| Generate labels / PDF (`label.generate`) | ✅ | 🟡 | ➖ | ➖ |
| Bulk import (`import.run`) | ✅ | ➖ | ➖ | ➖ |
| Manage users & memberships (`user.manage`) | ✅ | 🟡³ | ➖ | ➖ |
| Assign roles (`role.assign`) | ✅ | 🟡³ | ➖ | ➖ |
| View audit log (`audit.view`) | ✅ | 🟡⁴ | ➖ | ➖ |
| Manage tenant settings (`tenant.manage`) | ✅ | ➖ | ➖ | ➖ |
| Configure own notifications (`notify.self`) | ✅ | ✅ | ✅ | ✅ |

Footnotes:
1. Member may attach photos to assets they currently hold or are editing via a report.
2. Member checkout may require approval depending on the category's `requires_approval` flag (see below).
3. Project Lead may add/remove members and assign the **Member** role **only within their own project**; cannot create Admins.
4. Project Lead sees audit entries for their project's assets only.

## 4. Approval configuration (per category)

Reservation/checkout approval is **configurable per category** via
`Category.requires_approval`:

- `requires_approval = false` → self-service: `reservation.create` immediately
  yields an approved reservation / allows direct checkout.
- `requires_approval = true` → reservation enters `pending`; a user with
  `reservation.approve` in scope (Admin tenant-wide, or ProjectLead for that
  project's assets) must approve. General-pool assets requiring approval are
  approved by Admins.
- Override at asset level is possible later; MVP keeps it at category level.

## 5. Enforcement rules

- **Server-side only.** The UI hides disallowed actions, but every endpoint
  re-checks permission + scope. Never trust the client.
- **Tenant isolation is orthogonal and always applied first** — no permission can
  reach across tenants; Postgres RLS is the backstop.
- **Least privilege.** New users default to **Member**; elevated roles are granted
  explicitly.
- **Auditable.** Every `*.approve`, `*.override`, `user.manage`, `role.assign`,
  `stock.adjust`, and `asset.retire` writes an `AuditLog` entry.
