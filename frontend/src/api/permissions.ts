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
