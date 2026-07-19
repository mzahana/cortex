/**
 * Typed models matching the FROZEN auth contract (docs/api-and-ui.md,
 * "Contract frozen at T0.6"). Keep these in lockstep with the backend
 * serializers in `backend/apps/accounts/api.py` — do not add fields the
 * server doesn't send, and do not invent shapes for undocumented endpoints.
 */

export interface Tenant {
  id: number;
  slug: string;
  name: string;
}

/** One `Membership` row as returned inside `/me`. Tenant-wide memberships
 * have `project_id`/`project_name` null; project-scoped ones have both set. */
export interface MembershipSummary {
  role: string;
  role_name: string;
  project_id: number | null;
  project_name: string | null;
}

/**
 * `GET /api/v1/me` response — also the exact body returned by a successful
 * `POST /api/v1/auth/login` (docs/api-and-ui.md).
 */
export interface Me {
  id: number;
  email: string;
  name: string;
  tenant: Tenant;
  memberships: MembershipSummary[];
  /** Effective permission keys on the tenant's general pool (project=None):
   * union of every tenant-wide membership's role permissions. */
  permissions: string[];
  /** Keyed by project id (as a string, JSON object keys) — one entry per
   * project the user holds a project-scoped membership on. Each value is
   * that project's effective permission set (tenant-wide UNION'd with that
   * project's scoped grants). Projects with no scoped membership are absent. */
  project_permissions: Record<string, string[]>;
}

/** `POST /api/v1/auth/login` request body. `tenant` is the `Tenant.slug`. */
export interface LoginRequest {
  tenant: string;
  email: string;
  password: string;
}
