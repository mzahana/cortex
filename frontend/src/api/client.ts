/**
 * The single typed API client module (CLAUDE.md: "Typed API layer... components
 * never build URLs ad hoc"). Base path `/api/v1`, same-origin via nginx.
 * Session-cookie auth; CSRF token echoed on writes; RFC-7807 errors normalized
 * into `ApiError`.
 */
import type {
  Asset,
  AssetListParams,
  AssetWritePayload,
  Attachment,
  Category,
  CategoryWritePayload,
  CustomFieldDef,
  CustomFieldDefCreatePayload,
  ListParams,
  Location,
  LocationWritePayload,
  LoginRequest,
  Me,
  Paginated,
  Project,
  Tag,
} from "./types";
import { ApiError, type ProblemDetails } from "./problem";

const API_BASE = "/api/v1";

/** Must match `CSRF_COOKIE_NAME` in `backend/config/settings/base.py`. */
const CSRF_COOKIE_NAME = "cortex_csrftoken";
/** Must match `CSRF_HEADER_NAME` (`HTTP_X_CSRFTOKEN`) in the same file — the
 * actual wire header is `X-CSRFToken`. */
const CSRF_HEADER_NAME = "X-CSRFToken";

const WRITE_METHODS = new Set(["POST", "PATCH", "PUT", "DELETE"]);

function readCookie(name: string): string | null {
  const match = document.cookie
    .split("; ")
    .find((row) => row.startsWith(`${name}=`));
  return match ? decodeURIComponent(match.split("=").slice(1).join("=")) : null;
}

/** `GET /api/v1/auth/csrf` — unauthenticated, no body. Plants the
 * JS-readable `cortex_csrftoken` cookie so the client has a token to echo back
 * on the very first write (login), which has no session yet to rely on. */
async function ensureCsrfCookie(): Promise<void> {
  if (readCookie(CSRF_COOKIE_NAME)) return;
  await request<{ detail: string }>("/auth/csrf", { method: "GET" });
}

async function toApiError(response: Response): Promise<ApiError> {
  const contentType = response.headers.get("Content-Type") || "";
  if (contentType.includes("application/problem+json") || contentType.includes("application/json")) {
    try {
      const body = (await response.json()) as Partial<ProblemDetails>;
      return new ApiError({
        type: body.type ?? "about:blank",
        title: body.title ?? response.statusText,
        status: body.status ?? response.status,
        detail: body.detail,
        errors: body.errors,
        retry_after: body.retry_after ?? response.headers.get("Retry-After") ?? undefined,
      });
    } catch {
      // fall through to the generic fallback below
    }
  }
  return new ApiError({
    type: "about:blank",
    title: response.statusText || "Request failed",
    status: response.status,
    detail: `Request failed with status ${response.status}.`,
  });
}

interface RequestOptions extends Omit<RequestInit, "body"> {
  body?: unknown;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = (options.method || "GET").toUpperCase();
  const headers = new Headers(options.headers);

  if (options.body !== undefined) {
    headers.set("Content-Type", "application/json");
  }

  if (WRITE_METHODS.has(method)) {
    const token = readCookie(CSRF_COOKIE_NAME);
    if (token) headers.set(CSRF_HEADER_NAME, token);
  }

  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    method,
    headers,
    credentials: "include", // same-origin session cookie (cortex_sessionid)
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
  });

  if (!response.ok) {
    throw await toApiError(response);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
}

/** Builds a `?a=1&b=2` query string from a params object, dropping
 * null/undefined entries so callers can pass optional filters without
 * conditional spreads at every call site (CLAUDE.md: "components never
 * build URLs ad hoc" — this is the one place that does). */
function buildQuery(params?: ListParams): string {
  if (!params) return "";
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    search.set(key, String(value));
  }
  const qs = search.toString();
  return qs ? `?${qs}` : "";
}

/**
 * Server-side list endpoints are always paginated (`docs/api-and-ui.md` §1);
 * this walks every page and concatenates results. Used only for the
 * admin **tree** screens (Categories/Locations), which are bounded tenant
 * configuration — not asset lists — and need the *whole* tree client-side to
 * assemble parent/child structure. Never use this for `/assets` (CLAUDE.md:
 * "never load all assets"). Capped at `maxPages` as a defense-in-depth
 * safety net against a runaway loop / unexpectedly huge config set.
 */
async function fetchAllPages<T>(
  path: string,
  params?: ListParams,
  maxPages = 100,
): Promise<T[]> {
  const results: T[] = [];
  let page = 1;
  for (; page <= maxPages; page += 1) {
    const body = await request<Paginated<T>>(`${path}${buildQuery({ ...params, page })}`, {
      method: "GET",
    });
    results.push(...body.results);
    if (!body.next) break;
  }
  return results;
}

export const api = {
  /** `POST /api/v1/auth/login` — plants the CSRF cookie first if needed
   * (fresh session, nothing to echo yet), then logs in. On success the
   * response body is the same shape as `GET /me` and a session cookie is set. */
  async login(payload: LoginRequest): Promise<Me> {
    await ensureCsrfCookie();
    return request<Me>("/auth/login", { method: "POST", body: payload });
  },

  /** `POST /api/v1/auth/logout` — authenticated, CSRF-enforced via the
   * existing session. `204 No Content`. */
  async logout(): Promise<void> {
    await request<void>("/auth/logout", { method: "POST" });
  },

  /** `GET /api/v1/me` — authenticated. Throws `ApiError` (401/403) if not. */
  async me(): Promise<Me> {
    return request<Me>("/me", { method: "GET" });
  },

  // --- Categories (docs/api-and-ui.md "Structure"; apps.catalog.api.CategoryViewSet) ---
  // NOTE: every path below has a trailing slash — the router-registered
  // viewsets (`DefaultRouter`, `config/urls.py`) only resolve at
  // `/categories/`, `/categories/{id}/`, `/categories/{id}/fields/`, etc.;
  // Django's `APPEND_SLASH` 301-redirects a slash-less request, which a
  // `fetch()` POST cannot safely follow (the redirect drops the body/method
  // on most implementations) — verified against the real backend through
  // nginx while building this screen.

  /** `GET /api/v1/categories/` — one page. Read requires `asset.view`. */
  async listCategories(params?: ListParams): Promise<Paginated<Category>> {
    return request<Paginated<Category>>(`/categories/${buildQuery(params)}`, { method: "GET" });
  },

  /** Walks every page of `/api/v1/categories/` — see `fetchAllPages` doc
   * comment for why this is safe for the (bounded) category tree. */
  async listAllCategories(params?: ListParams): Promise<Category[]> {
    return fetchAllPages<Category>("/categories/", params);
  },

  async getCategory(id: number): Promise<Category> {
    return request<Category>(`/categories/${id}/`, { method: "GET" });
  },

  /** `POST /api/v1/categories/` — requires `category.manage`. */
  async createCategory(payload: CategoryWritePayload): Promise<Category> {
    return request<Category>("/categories/", { method: "POST", body: payload });
  },

  /** `PATCH /api/v1/categories/{id}/` — requires `category.manage`. */
  async updateCategory(id: number, payload: Partial<CategoryWritePayload>): Promise<Category> {
    return request<Category>(`/categories/${id}/`, { method: "PATCH", body: payload });
  },

  /** `DELETE /api/v1/categories/{id}/` — requires `category.manage`. A
   * category with children or referencing rows 409s (RFC-7807, `ProtectedError`
   * -> `apps.common.errors._protected_error_response`) — surface `err.status
   * === 409` as "still in use" in the UI, never as an unhandled error. */
  async deleteCategory(id: number): Promise<void> {
    await request<void>(`/categories/${id}/`, { method: "DELETE" });
  },

  /** `GET /api/v1/categories/{id}/fields/` — the category's `CustomFieldDef`
   * list, already ordered (`order`, then `id`) server-side. Not paginated
   * (`apps.catalog.api.CategoryViewSet.fields` returns a plain list). */
  async listCategoryFields(categoryId: number): Promise<CustomFieldDef[]> {
    return request<CustomFieldDef[]>(`/categories/${categoryId}/fields/`, { method: "GET" });
  },

  /** `POST /api/v1/categories/{id}/fields/` — requires `category.manage`.
   * NOTE (flagged for backend-engineer): there is no documented endpoint to
   * edit/reorder/delete an existing field def — only create. */
  async createCategoryField(
    categoryId: number,
    payload: CustomFieldDefCreatePayload,
  ): Promise<CustomFieldDef> {
    return request<CustomFieldDef>(`/categories/${categoryId}/fields/`, {
      method: "POST",
      body: payload,
    });
  },

  // --- Locations (docs/api-and-ui.md "Structure"; apps.catalog.api.LocationViewSet) ---

  async listLocations(params?: ListParams): Promise<Paginated<Location>> {
    return request<Paginated<Location>>(`/locations/${buildQuery(params)}`, { method: "GET" });
  },

  /** Walks every page — see `fetchAllPages` doc comment. */
  async listAllLocations(params?: ListParams): Promise<Location[]> {
    return fetchAllPages<Location>("/locations/", params);
  },

  async getLocation(id: number): Promise<Location> {
    return request<Location>(`/locations/${id}/`, { method: "GET" });
  },

  /** `POST /api/v1/locations/` — requires `location.manage`. */
  async createLocation(payload: LocationWritePayload): Promise<Location> {
    return request<Location>("/locations/", { method: "POST", body: payload });
  },

  /** `PATCH /api/v1/locations/{id}/` — requires `location.manage`. */
  async updateLocation(id: number, payload: Partial<LocationWritePayload>): Promise<Location> {
    return request<Location>(`/locations/${id}/`, { method: "PATCH", body: payload });
  },

  /** `DELETE /api/v1/locations/{id}/` — requires `location.manage`. Same
   * `ProtectedError` -> `409` behavior as `deleteCategory` above. */
  async deleteLocation(id: number): Promise<void> {
    await request<void>(`/locations/${id}/`, { method: "DELETE" });
  },

  // --- Projects (docs/api-and-ui.md "Structure"; apps.catalog.api.ProjectViewSet) ---
  // Only used here to populate the Asset List's project filter dropdown —
  // bounded tenant config (a handful of projects), same reasoning as
  // Categories/Locations above, never an asset list.

  /** Walks every page of `/api/v1/projects/` — see `fetchAllPages` doc comment. */
  async listAllProjects(params?: ListParams): Promise<Project[]> {
    return fetchAllPages<Project>("/projects/", params);
  },

  /** `GET /api/v1/projects/{id}/` — `apps.catalog.api.ProjectViewSet` is a
   * full `ModelViewSet` (registered CRUD per `docs/api-and-ui.md`), so the
   * detail route exists; resolving a single asset's project name here (T1.7
   * carried fix) is one request instead of walking every project page just
   * to `.find()` one row (the prior `AssetDetailScreen` approach). 404s if
   * the id doesn't exist or isn't this tenant's (never distinguishes the
   * two, R4-safe, same as `getAsset`). */
  async getProject(id: number): Promise<Project> {
    return request<Project>(`/projects/${id}/`, { method: "GET" });
  },

  // --- Tags (docs/api-and-ui.md "Structure"; apps.catalog.api.TagViewSet, read-only) ---
  // Same "bounded catalog config, not an asset list" reasoning as Projects above.

  /** Walks every page of `/api/v1/tags/`. */
  async listAllTags(params?: ListParams): Promise<Tag[]> {
    return fetchAllPages<Tag>("/tags/", params);
  },

  // --- Assets (docs/api-and-ui.md "Assets"; apps.assets.api.AssetViewSet) ---
  // NOTE (T1.6, CLAUDE.md "never load all assets"): unlike the catalog
  // helpers above, there is deliberately NO `listAllAssets` / `fetchAllPages`
  // wrapper here — assets are unbounded (10k+ per T1.8's perf seed). Every
  // caller gets back exactly ONE server-side page and must ask for more
  // explicitly (`?page=`/`?cursor=`), which is what makes infinite-scroll/
  // virtualized rendering in `AssetListScreen` actually server-side rather
  // than a client-side illusion over an already-fully-loaded array.

  /** `GET /api/v1/assets/` — one page (bounded `?page`/`?page_size`, default
   * mode) or one cursor-window (`?cursor=`, opt-in — see `AssetListParams`
   * doc comment) depending on which params are supplied. Read requires
   * `asset.view`, further row-scoped per `docs/rbac.md` §1 server-side. */
  async listAssets(params?: AssetListParams): Promise<Paginated<Asset>> {
    return request<Paginated<Asset>>(`/assets/${buildQuery(params)}`, { method: "GET" });
  },

  /** `GET /api/v1/assets/{id}/` — full detail (same `AssetSerializer` shape
   * as a list row, see `Asset` type doc comment). 404s if the id doesn't
   * exist or isn't visible under the caller's tenant/scope (never
   * distinguishes the two, R4-safe). */
  async getAsset(id: number): Promise<Asset> {
    return request<Asset>(`/assets/${id}/`, { method: "GET" });
  },

  /** `POST /api/v1/assets/` — requires `asset.create` (scoped). Body incl.
   * custom field values (`custom_field_values`) and inline `tags` (names,
   * created on the fly server-side). Server 400s (RFC-7807, `errors` keyed
   * by field, `custom_field_values` itself keyed by field-def `key` for
   * per-custom-field errors) are the authority — this is UX-only client-side
   * pre-validation in `AssetFormScreen`, never trusted alone. */
  async createAsset(payload: AssetWritePayload): Promise<Asset> {
    return request<Asset>("/assets/", { method: "POST", body: payload });
  },

  /** `PATCH /api/v1/assets/{id}/` — requires `asset.edit` (scoped). Same
   * payload shape/validation as `createAsset` above; omitting a key leaves it
   * untouched server-side (partial update) EXCEPT `custom_field_values`,
   * which — once present in the body at all, even as `{}` — fully replaces
   * the asset's custom values and re-validates the category's full required
   * set (see `AssetWritePayload` doc comment). */
  async updateAsset(id: number, payload: AssetWritePayload): Promise<Asset> {
    return request<Asset>(`/assets/${id}/`, { method: "PATCH", body: payload });
  },

  /** `POST /api/v1/assets/{id}/retire/` — requires `asset.retire` (scoped).
   * Idempotent: retiring an already-retired asset 200s as a no-op rather
   * than erroring (`apps.assets.api.AssetViewSet.retire`). Audited
   * server-side (docs/rbac.md §5). */
  async retireAsset(id: number): Promise<Asset> {
    return request<Asset>(`/assets/${id}/retire/`, { method: "POST" });
  },

  /** `POST /api/v1/assets/{id}/attachments/` — requires `asset.attach`
   * (scoped; footnote 1: a Member may attach to assets they hold/are
   * editing via a report). Multipart, so this bypasses `request()`'s
   * JSON-only body handling entirely — same CSRF-cookie-echo rule applies. */
  async uploadAssetAttachment(
    assetId: number,
    file: File,
    kind: "photo" | "doc" = "photo",
  ): Promise<Attachment> {
    const formData = new FormData();
    formData.append("file", file);
    formData.append("kind", kind);

    const headers = new Headers();
    const token = readCookie(CSRF_COOKIE_NAME);
    if (token) headers.set(CSRF_HEADER_NAME, token);

    const response = await fetch(`${API_BASE}/assets/${assetId}/attachments/`, {
      method: "POST",
      credentials: "include",
      headers,
      body: formData,
    });
    if (!response.ok) throw await toApiError(response);
    return (await response.json()) as Attachment;
  },
};

export { ApiError };
