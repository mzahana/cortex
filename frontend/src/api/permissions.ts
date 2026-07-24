import type { Me } from "./types";

/**
 * UI-gating helper only (CLAUDE.md: "Hide actions the user's effective
 * permissions ... don't allow, but assume the server re-checks — a 403 is a
 * normal, handled outcome, not a bug"). `Category`/`Location` are tenant-wide
 * admin config with no project scope of their own (`apps.catalog.permissions`
 * module docstring), so the check is always against the tenant-wide
 * `permissions` pool from `/me`, never `project_permissions` — this mirrors
 * the server's own `TenantWideReadOrManage` (`project=None` always).
 *
 * This function is NOT a security boundary. It only decides whether to show
 * an edit affordance; the server is the sole authority and will 403 an
 * unauthorized write regardless of what this returns.
 */
export function hasPermission(me: Me | null | undefined, key: string): boolean {
  return !!me?.permissions.includes(key);
}

export const CATEGORY_MANAGE = "category.manage";
export const LOCATION_MANAGE = "location.manage";

// `docs/rbac.md` §3 asset action keys (T1.6: Asset List/Detail action
// gating). Unlike Category/Location above, assets ARE project-scoped
// (`Asset.project`, nullable = general pool) — a real scope check needs
// `hasAssetPermission` below, not the tenant-wide-only `hasPermission`.
export const ASSET_VIEW = "asset.view";
export const ASSET_EXPORT = "asset.export";
export const ASSET_CREATE = "asset.create";
export const ASSET_EDIT = "asset.edit";
export const ASSET_RETIRE = "asset.retire";
export const ASSET_ATTACH = "asset.attach";

// `docs/rbac.md` §3 stock/reorder action keys (T2.4: Stock screen gating).
// Stock/reorder rows are project-scoped through their underlying `Asset`
// (`apps.stock.permissions` module docstring) — same scope resolution as
// asset actions, so `hasAssetPermission`/`hasAnyAssetPermission` below are
// reused directly (not re-implemented) for these keys.
export const STOCK_ADJUST = "stock.adjust";
export const STOCK_CONSUME = "stock.consume";
export const REORDER_REQUEST = "reorder.request";
export const REORDER_APPROVE = "reorder.approve";

// `docs/rbac.md` §3/§4 reservation action keys (T3.4: Reservations Calendar +
// Approvals screen gating). Reservations are project-scoped through their
// underlying `Asset` (`apps.reservations.permissions` module docstring) —
// same scope resolution as asset/stock actions, so `hasAssetPermission`/
// `hasAnyAssetPermission` are reused directly (not re-implemented).
export const RESERVATION_CREATE = "reservation.create";
export const RESERVATION_APPROVE = "reservation.approve";

// `docs/rbac.md` §3 checkout action keys (T3.5: My Items + Asset Detail
// check-out/check-in gating). Checkouts are project-scoped through their
// underlying `Asset` (`apps.reservations.checkout.CheckoutPermission`
// docstring) — same scope resolution as asset/stock/reservation actions, so
// `hasAssetPermission`/`hasAnyAssetPermission` are reused directly (not
// re-implemented). Note: `checkout.manage` gates BOTH check-out and
// self-service check-in (the server additionally requires the caller be the
// checkout's holder for check-in specifically — a client-side check this
// module can't express without knowing which checkout is "mine", which the
// caller resolves itself); `checkout.override` gates force-return only.
export const CHECKOUT_MANAGE = "checkout.manage";
export const CHECKOUT_OVERRIDE = "checkout.override";

// `docs/rbac.md` §3 label action key (T4.5: Labels screen gating). Labels
// are project-scoped through each requested asset's own `project`
// (`apps.labels.permissions`/`apps.labels.api` — Admin tenant-wide,
// ProjectLead within their own project's assets only) — same scope
// resolution as asset/stock/reservation/checkout actions, so
// `hasAssetPermission`/`hasAnyAssetPermission` are reused directly (not
// re-implemented) both for the entry-point gate (any asset picked) and for
// per-asset selection gating in the picker.
export const LABEL_GENERATE = "label.generate";

// `docs/rbac.md` §3 audit/notification action keys (T5.6: Audit Log + My
// Notifications screen gating). `audit.view` is tenant-wide-only for the
// purpose of the nav-link gate below — a pure ProjectLead's grant IS
// project-scoped (docs/rbac.md footnote 4: "their project's assets only"),
// but there is no single "asset" to scope the nav link against here, so this
// checks tenant-wide OR any project scope (`hasAnyAssetPermission`-style),
// same presentation-only caveat as every other helper in this module — the
// server's `AuditLogViewSet.get_queryset` is the real per-row authority.
export const AUDIT_VIEW = "audit.view";
// `notify.self` is granted tenant-wide to every role (docs/rbac.md §3) — a
// plain `hasPermission` check against the tenant-wide pool is sufficient,
// there is no scoped variant.
export const NOTIFY_SELF = "notify.self";

// `docs/rbac.md` §3 bulk-import action key (T6.2: Import screen gating).
// `import.run` is Admin-only, tenant-wide, with no ProjectLead/scoped
// reading at all (`apps.imports.permissions.ImportRunPermission` docstring)
// — a plain tenant-wide `hasPermission` check is sufficient, unlike the
// asset/stock/reservation/checkout/label keys above.
export const IMPORT_RUN = "import.run";

/** UI-gating helper for the Import screen entry point (presentation only,
 * same caveat as every other helper in this module): true if the caller
 * holds `import.run` tenant-wide. */
export function hasImportRunPermission(me: Me | null | undefined): boolean {
  return hasPermission(me, IMPORT_RUN);
}

/** UI-gating helper for the Audit Log nav link (presentation only, same
 * caveat as `hasAssetPermission`): true if the caller holds `audit.view`
 * tenant-wide OR scoped to at least one project (a ProjectLead should still
 * see the entry point — the server narrows what they actually see inside). */
export function hasAuditViewPermission(me: Me | null | undefined): boolean {
  return hasAnyAssetPermission(me, AUDIT_VIEW);
}

/**
 * UI-gating helper for an action on a SPECIFIC asset (NOT a security
 * boundary — same caveat as `hasPermission` above). Mirrors the server's own
 * scope resolution (`docs/rbac.md` §1): a tenant-wide grant of `key` always
 * qualifies; otherwise, for a project-assigned asset (`projectId` set), only
 * that project's own scoped grant qualifies — a general-pool asset
 * (`projectId === null`) is governed by the tenant-wide pool only, a
 * ProjectLead's project-scoped power never reaches it.
 */
export function hasAssetPermission(
  me: Me | null | undefined,
  key: string,
  projectId: number | null,
): boolean {
  if (!me) return false;
  if (me.permissions.includes(key)) return true;
  if (projectId === null) return false;
  return !!me.project_permissions[String(projectId)]?.includes(key);
}

/**
 * UI-gating helper for an entry point that doesn't (yet) target a specific
 * asset/project — e.g. the Asset List's "New asset" button (T1.7). `true` if
 * the caller holds `key` tenant-wide OR has it scoped to at least one
 * project (a Project Lead who can only create within their own project
 * should still see the entry point; the create form's own project picker,
 * plus the server's real scoped check on submit, is what actually narrows
 * it — this is presentation only, same caveat as `hasAssetPermission`).
 */
export function hasAnyAssetPermission(me: Me | null | undefined, key: string): boolean {
  if (!me) return false;
  if (me.permissions.includes(key)) return true;
  return Object.values(me.project_permissions).some((perms) => perms.includes(key));
}

/** UI-gating helper for the Asset List's "Export CSV" button (T6.2). `asset.
 * export` is scoped exactly like the other asset action keys above (Admin/
 * Member tenant-wide, ProjectLead scoped to their own project's assets,
 * `apps.imports.exports.AssetExportView` docstring) — an entry-point gate
 * with no single target asset/project, so this is `hasAnyAssetPermission`
 * under the hood, same caveat as every other helper in this module. */
export function hasAssetExportPermission(me: Me | null | undefined): boolean {
  return hasAnyAssetPermission(me, ASSET_EXPORT);
}

// Users & Roles admin action key (`apps.accounts.permissions.
// UserManagementPermission`/`apps.rbac.permissions.MembershipPermission`).
// `user.manage` is held tenant-wide by Admin and project-scoped by
// ProjectLead (docs/rbac.md §3) -- same "any scope shows the entry point"
// reasoning as `hasAuditViewPermission`: a ProjectLead should still see the
// nav entry, scoped to their own project by what the server actually
// returns from `GET /api/v1/memberships` (`get_viewable_project_scope`).
export const USER_MANAGE = "user.manage";

/** UI-gating helper for the Users & Roles nav link (presentation only, same
 * caveat as every other helper in this module): true if the caller holds
 * `user.manage` tenant-wide OR scoped to at least one project. */
export function hasUserManagePermission(me: Me | null | undefined): boolean {
  return hasAnyAssetPermission(me, USER_MANAGE);
}
