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
  Checkout,
  CheckoutCreatePayload,
  CheckoutListParams,
  CheckinPayload,
  CustomFieldDef,
  CustomFieldDefCreatePayload,
  CustomFieldDefUpdatePayload,
  ListParams,
  Location,
  LocationWritePayload,
  LoginRequest,
  Me,
  Paginated,
  Project,
  ReorderRequest,
  ReorderRequestCreatePayload,
  ReorderRequestListParams,
  ReorderRequestUpdatePayload,
  Reservation,
  ReservationCreatePayload,
  ReservationListParams,
  StockItem,
  StockListParams,
  StockTxnPayload,
  StockTxnResponse,
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

  /** `POST /api/v1/categories/{id}/fields/` — requires `category.manage`. */
  async createCategoryField(
    categoryId: number,
    payload: CustomFieldDefCreatePayload,
  ): Promise<CustomFieldDef> {
    return request<CustomFieldDef>(`/categories/${categoryId}/fields/`, {
      method: "POST",
      body: payload,
    });
  },

  /** `PATCH /api/v1/categories/{cat_id}/fields/{field_id}/` — requires
   * `category.manage`. `fieldId` is re-scoped server-side under `catId` (a
   * field from another category/tenant 404s). See
   * `CustomFieldDefUpdatePayload` doc comment for the data-type-change and
   * uniqueness policies enforced server-side. */
  async updateCategoryField(
    catId: number,
    fieldId: number,
    payload: CustomFieldDefUpdatePayload,
  ): Promise<CustomFieldDef> {
    return request<CustomFieldDef>(`/categories/${catId}/fields/${fieldId}/`, {
      method: "PATCH",
      body: payload,
    });
  },

  /** `DELETE /api/v1/categories/{cat_id}/fields/{field_id}/` — requires
   * `category.manage`, `204`. **Destructive**: cascades — deletes every
   * `AssetFieldValue` row referencing this field def on every asset in the
   * category (DB `on_delete=CASCADE`), with no "block if in use" guard. The
   * UI must confirm before calling this (see `CategoryFieldsPanel`'s
   * `ConfirmDeleteModal` usage). */
  async deleteCategoryField(catId: number, fieldId: number): Promise<void> {
    await request<void>(`/categories/${catId}/fields/${fieldId}/`, { method: "DELETE" });
  },

  /** `POST /api/v1/categories/{id}/fields/reorder/` — requires
   * `category.manage`. `orderedIds` must be *exactly* the category's current
   * field def ids (no more, no fewer, no duplicates) — position becomes the
   * new 0-indexed `order`. A partial/mismatched set is rejected whole with a
   * `400` (single atomic call, never a per-field `PATCH .../order`). Returns
   * the reordered list (same shape as `listCategoryFields`). */
  async reorderCategoryFields(catId: number, orderedIds: number[]): Promise<CustomFieldDef[]> {
    return request<CustomFieldDef[]>(`/categories/${catId}/fields/reorder/`, {
      method: "POST",
      body: { order: orderedIds },
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

  // --- Stock / consumables (docs/api-and-ui.md "Stock"; apps.stock.api) ---
  // NOTE: trailing slashes for the same `APPEND_SLASH` reason as Categories/
  // Locations above (router-registered viewsets).

  /** `GET /api/v1/stock/` — one page. Read requires `asset.view`, scope-aware
   * server-side (same union-of-memberships rule as `listAssets`). Never
   * walks all pages (CLAUDE.md: server-side lists) — `?low_stock=true` is the
   * documented low-stock filter. */
  async listStock(params?: StockListParams): Promise<Paginated<StockItem>> {
    return request<Paginated<StockItem>>(`/stock/${buildQuery(params)}`, { method: "GET" });
  },

  /** `GET /api/v1/stock/{id}/` — detail. */
  async getStockItem(id: number): Promise<StockItem> {
    return request<StockItem>(`/stock/${id}/`, { method: "GET" });
  },

  /** `POST /api/v1/stock/{id}/txn/` — apply a ledger transaction
   * (receive/consume/adjust/correction). Requires `stock.adjust` (receive/
   * adjust/correction) or `stock.consume` (consume), scoped to the stock
   * item's asset's project — server re-checks regardless of UI gating.
   * Rejects a delta that would drive `quantity_on_hand` negative with a
   * `400` (RFC-7807, surfaced via `err.problem`). Returns the updated
   * `StockItem` + the created ledger row + a `low_stock` flag. */
  async postStockTxn(stockItemId: number, payload: StockTxnPayload): Promise<StockTxnResponse> {
    return request<StockTxnResponse>(`/stock/${stockItemId}/txn/`, {
      method: "POST",
      body: payload,
    });
  },

  /** `GET /api/v1/reorder-requests/` — one page. Read requires `asset.view`,
   * scope-aware. `?status=` filters to one lifecycle stage. */
  async listReorderRequests(params?: ReorderRequestListParams): Promise<Paginated<ReorderRequest>> {
    return request<Paginated<ReorderRequest>>(`/reorder-requests/${buildQuery(params)}`, {
      method: "GET",
    });
  },

  /** `POST /api/v1/reorder-requests/` — requires `reorder.request`, scoped
   * to the target stock item's asset's project. `requested_by`/`status`
   * (`open`) are server-derived. */
  async createReorderRequest(payload: ReorderRequestCreatePayload): Promise<ReorderRequest> {
    return request<ReorderRequest>("/reorder-requests/", { method: "POST", body: payload });
  },

  /** `PATCH /api/v1/reorder-requests/{id}/` — status transitions
   * (`open -> approved -> ordered -> received`, `cancelled` from any
   * non-terminal state) or a plain field edit. Approval-track transitions
   * require `reorder.approve` (scoped); `cancelled` and plain edits may also
   * be done by the original requester on their own still-open request. An
   * invalid transition 400s (RFC-7807) — this is the server's authority, not
   * pre-validated here beyond basic UI gating. */
  async updateReorderRequest(
    id: number,
    payload: ReorderRequestUpdatePayload,
  ): Promise<ReorderRequest> {
    return request<ReorderRequest>(`/reorder-requests/${id}/`, { method: "PATCH", body: payload });
  },

  // --- Reservations (docs/api-and-ui.md "Reservations & checkout";
  // apps.reservations.api.ReservationViewSet) ---
  // Trailing slashes, same `APPEND_SLASH` reason as Categories/Locations/Stock.

  /** `GET /api/v1/reservations/` — one page. Read requires `asset.view`,
   * scope-aware server-side (same union-of-memberships rule as `listAssets`/
   * `listStock`). `?from&to` is the calendar-feed window; `?status=` filters
   * to one lifecycle stage (used by both the Calendar and the Approvals
   * screen — approvals passes `status=pending`). Never walks all pages
   * (CLAUDE.md: server-side lists). */
  async listReservations(params?: ReservationListParams): Promise<Paginated<Reservation>> {
    return request<Paginated<Reservation>>(`/reservations/${buildQuery(params)}`, {
      method: "GET",
    });
  },

  /** `GET /api/v1/reservations/{id}/` — detail. */
  async getReservation(id: number): Promise<Reservation> {
    return request<Reservation>(`/reservations/${id}/`, { method: "GET" });
  },

  /** `POST /api/v1/reservations/` — requires `reservation.create`, scoped to
   * the target asset's project. The conflict pre-check (F4) and the
   * per-user-cap/window checks live server-side
   * (`apps.reservations.services.create_reservation`); an overlapping window
   * 409s (`ReservationConflict`, `err.status === 409`) — surface that inline
   * as a conflict message, not a generic error. */
  async createReservation(payload: ReservationCreatePayload): Promise<Reservation> {
    return request<Reservation>("/reservations/", { method: "POST", body: payload });
  },

  /** `POST /api/v1/reservations/{id}/approve/` — requires `reservation.approve`,
   * scoped to the reservation's asset's project (general-pool assets:
   * Admins only). Only a `pending` reservation can be approved — the server
   * 400s otherwise (`err.problem.errors.status`). */
  async approveReservation(id: number, note?: string): Promise<Reservation> {
    return request<Reservation>(`/reservations/${id}/approve/`, {
      method: "POST",
      body: note ? { approval_note: note } : {},
    });
  },

  /** `POST /api/v1/reservations/{id}/reject/` — same scope/permission as
   * `approveReservation`. */
  async rejectReservation(id: number, note?: string): Promise<Reservation> {
    return request<Reservation>(`/reservations/${id}/reject/`, {
      method: "POST",
      body: note ? { approval_note: note } : {},
    });
  },

  /** `POST /api/v1/reservations/{id}/cancel/` — the requester (own booking)
   * or a scoped approver. Only `pending`/`approved` reservations are
   * cancellable — the server 400s otherwise. */
  async cancelReservation(id: number): Promise<Reservation> {
    return request<Reservation>(`/reservations/${id}/cancel/`, { method: "POST" });
  },

  // --- Checkouts (docs/api-and-ui.md "Reservations & checkout";
  // apps.reservations.checkout.CheckoutViewSet) ---
  // Trailing slashes, same `APPEND_SLASH` reason as Categories/Locations/Stock.

  /** `GET /api/v1/checkouts/` — one page. Read requires `checkout.manage` OR
   * `checkout.override` in any scope, further row-scoped server-side to the
   * caller's own checkouts UNION their scope (docs/rbac.md §1) — see
   * `CheckoutListParams` doc comment: there is no `?user=me` param, the
   * server already includes "my own" regardless of scope. `?open=true`/
   * `?overdue=true` are the documented filters (T3.5 My Items screen). */
  async listCheckouts(params?: CheckoutListParams): Promise<Paginated<Checkout>> {
    return request<Paginated<Checkout>>(`/checkouts/${buildQuery(params)}`, { method: "GET" });
  },

  /** `GET /api/v1/checkouts/{id}/` — detail. */
  async getCheckout(id: number): Promise<Checkout> {
    return request<Checkout>(`/checkouts/${id}/`, { method: "GET" });
  },

  /** `POST /api/v1/checkouts/` — requires `checkout.manage`, scoped to the
   * target asset's project. Rejects a consumable asset or one not currently
   * `available`/`reserved` with a `400` (RFC-7807) — this is UX-only
   * pre-validation, the server re-checks under a row lock regardless. */
  async createCheckout(payload: CheckoutCreatePayload): Promise<Checkout> {
    return request<Checkout>("/checkouts/", { method: "POST", body: payload });
  },

  /** `POST /api/v1/checkouts/{id}/checkin/` — self-service only: the caller
   * must be the checkout's holder (object-level RBAC, `apps.reservations.
   * checkout.CheckoutPermission.has_object_permission`) — someone else must
   * use `overrideReturnCheckout` (`checkout.override`) instead. Idempotent:
   * calling this twice is a documented no-op, not an error. */
  async checkinCheckout(id: number, payload?: CheckinPayload): Promise<Checkout> {
    return request<Checkout>(`/checkouts/${id}/checkin/`, {
      method: "POST",
      body: payload ?? {},
    });
  },

  /** `POST /api/v1/checkouts/{id}/override-return/` — requires
   * `checkout.override`, scoped to the checkout's asset's project. Force-
   * return by someone other than the holder; audited under the
   * `checkout.override` key. Idempotent, same no-op rule as `checkinCheckout`. */
  async overrideReturnCheckout(id: number, payload?: CheckinPayload): Promise<Checkout> {
    return request<Checkout>(`/checkouts/${id}/override-return/`, {
      method: "POST",
      body: payload ?? {},
    });
  },
};

export { ApiError };
