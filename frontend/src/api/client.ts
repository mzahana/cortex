/**
 * The single typed API client module (CLAUDE.md: "Typed API layer... components
 * never build URLs ad hoc"). Base path `/api/v1`, same-origin via nginx.
 * Session-cookie auth; CSRF token echoed on writes; RFC-7807 errors normalized
 * into `ApiError`.
 */
import type {
  AppUser,
  Asset,
  AssetListParams,
  AssetWritePayload,
  Attachment,
  AuditLogEntry,
  AuditLogListParams,
  Category,
  CategoryWritePayload,
  ChangePasswordRequest,
  Checkout,
  CheckoutCreatePayload,
  CheckoutListParams,
  CheckinPayload,
  CreatedUser,
  CreateUserPayload,
  ForgotPasswordRequest,
  PasswordResetConfirmRequest,
  CustomFieldDef,
  CustomFieldDefCreatePayload,
  CustomFieldDefUpdatePayload,
  DashboardSummary,
  EmailSettings,
  EmailSettingsTestResult,
  EmailSettingsUpdate,
  Expense,
  ExpenseAttachment,
  ExpenseCategory,
  ExpenseCategoryListParams,
  ExpenseListParams,
  ExpenseWritePayload,
  ImportJob,
  ImportMapping,
  Job,
  LabelGenerateRequest,
  ListParams,
  Location,
  LocationWritePayload,
  LoginRequest,
  Me,
  Membership,
  MembershipCreatePayload,
  MembershipListParams,
  MembershipUpdatePayload,
  NotificationPref,
  NotificationPrefUpdatePayload,
  Paginated,
  Project,
  ProjectCreatePayload,
  ProjectDetail,
  ProjectDocument,
  ProjectDocumentKind,
  ProjectListParams,
  ProjectUpdatePayload,
  ReorderRequest,
  ReorderRequestCreatePayload,
  ReorderRequestListParams,
  ReorderRequestUpdatePayload,
  Reservation,
  ReservationCreatePayload,
  ReservationListParams,
  Role,
  StockItem,
  StockItemCreatePayload,
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

  /** `POST /api/v1/me/password` — self-service password change. `204` on
   * success; the server refreshes the session auth hash so the user stays
   * logged in. `400` with `errors.current_password`/`errors.new_password`
   * carries field-level messages the form surfaces. */
  async changePassword(payload: ChangePasswordRequest): Promise<void> {
    await request<void>("/me/password", { method: "POST", body: payload });
  },

  /** `POST /api/v1/auth/password-reset/request` — unauthenticated. Plants the
   * CSRF cookie first (like `login`, there may be no session yet). ALWAYS
   * resolves 200 with a generic message regardless of whether the account
   * exists (no enumeration). */
  async requestPasswordReset(payload: ForgotPasswordRequest): Promise<{ detail: string }> {
    await ensureCsrfCookie();
    return request<{ detail: string }>("/auth/password-reset/request", {
      method: "POST",
      body: payload,
    });
  },

  /** `POST /api/v1/auth/password-reset/confirm` — unauthenticated. `204` on
   * success; `400` (`invalid-reset-token`) if the link is bad/expired/used, or
   * `errors.new_password` if the chosen password is too weak. */
  async confirmPasswordReset(payload: PasswordResetConfirmRequest): Promise<void> {
    await ensureCsrfCookie();
    await request<void>("/auth/password-reset/confirm", { method: "POST", body: payload });
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

  /** `POST /api/v1/projects/` — requires `tenant.manage` (Admin-only
   * structural create, unchanged by M7 — see `ProjectCreatePayload` doc
   * comment for why the full grant-metadata field set is accepted here too,
   * not just `name`/`lead_user`/`is_active`). */
  async createProject(payload: ProjectCreatePayload): Promise<Project> {
    return request<Project>("/projects/", { method: "POST", body: payload });
  },

  /** `PATCH /api/v1/projects/{id}/` — same `tenant.manage` gate as create. */
  async updateProject(
    id: number,
    payload: Partial<{ name: string; lead_user: number | null; is_active: boolean }>,
  ): Promise<Project> {
    return request<Project>(`/projects/${id}/`, {
      method: "PATCH",
      body: payload,
    });
  },

  /** `DELETE /api/v1/projects/{id}/` — same `tenant.manage` gate as create.
   * A project referenced by assets/reservations/etc. via `PROTECT` FKs
   * 409s, same `ProtectedError` behavior as `deleteCategory`/`deleteLocation`. */
  async deleteProject(id: number): Promise<void> {
    await request<void>(`/projects/${id}/`, { method: "DELETE" });
  },

  // --- Project hub (M7, docs/tasks/M7-project-grants.md; `apps.projects.api`)
  // --- Grant metadata + budget rollup, project-scoped assets/expenses/
  // documents, report PDF job, and CSV export. Distinct from the bounded
  // catalog helpers above (`listAllProjects`/`createProject`/`updateProject`/
  // `deleteProject`, which stay the thin Admin CRUD contract unchanged) —
  // these are the richer M7 hub surface (`ProjectDetail`, budget/spend
  // rollup, nested sub-resources).

  /** `GET /api/v1/projects/` — one server-side page (the Projects hub LIST
   * screen; NOT the bounded `listAllProjects` walk-every-page helper used
   * for filter dropdowns elsewhere — CLAUDE.md: never assume a tenant's
   * project count stays small forever). `budget_total` is redacted to
   * `null` per-row for a caller without project-scoped `expense.view` — see
   * `Project` type doc comment. */
  async listProjects(params?: ProjectListParams): Promise<Paginated<Project>> {
    return request<Paginated<Project>>(`/projects/${buildQuery(params)}`, { method: "GET" });
  },

  /** `GET /api/v1/projects/{id}/` — the M7 hub detail (`ProjectDetail`:
   * grant metadata + `spent`/`remaining`/`spend_by_category`, all four
   * redacted to `null` together under the same per-project `expense.view`
   * gate as `budget_total`). Requires `project.view` (tenant-wide-grantable)
   * for the row itself to 200 at all — see `ProjectDetail` doc comment. */
  async getProjectDetail(id: number): Promise<ProjectDetail> {
    return request<ProjectDetail>(`/projects/${id}/`, { method: "GET" });
  },

  /** `PATCH /api/v1/projects/{id}/` — requires `project.manage` scoped to
   * this project (Admin tenant-wide, or that project's own Lead). Edits
   * grant metadata/budget; partial update, every field optional. */
  async updateProjectDetail(id: number, payload: ProjectUpdatePayload): Promise<ProjectDetail> {
    return request<ProjectDetail>(`/projects/${id}/`, { method: "PATCH", body: payload });
  },

  /** `GET /api/v1/projects/{id}/assets/` — reuses the SAME pagination +
   * `AssetSerializer` + filter/search/ordering as `GET /api/v1/assets`,
   * scoped to this project server-side (`apps.projects.api.ProjectViewSet.
   * assets`). Requires `project.view` scoped to this project — a guessed/
   * cross-tenant/other-Lead's project id 403s/404s here, never leaks a list.
   * This is what lets the project hub's Assets tab reuse `useAssetList`/
   * `AssetListView` unchanged (same `Paginated<Asset>` envelope, same
   * `AssetListParams` query shape) instead of a parallel implementation. */
  async listProjectAssets(projectId: number, params?: AssetListParams): Promise<Paginated<Asset>> {
    return request<Paginated<Asset>>(`/projects/${projectId}/assets/${buildQuery(params)}`, {
      method: "GET",
    });
  },

  /** `GET /api/v1/projects/{id}/expenses/` — one page, `?category=`/
   * `?date_from=`/`?date_to=` filters (`ExpenseFilterSet`). Requires
   * `expense.view` scoped to this project (Lead-of-that-project, or Admin)
   * — the project-scoped financial boundary, same gate as the budget rollup. */
  async listProjectExpenses(
    projectId: number,
    params?: ExpenseListParams,
  ): Promise<Paginated<Expense>> {
    return request<Paginated<Expense>>(`/projects/${projectId}/expenses/${buildQuery(params)}`, {
      method: "GET",
    });
  },

  /** `POST /api/v1/projects/{id}/expenses/` — requires `expense.manage`
   * scoped to this project. `project`/`created_by` are server-derived. */
  async createExpense(projectId: number, payload: ExpenseWritePayload): Promise<Expense> {
    return request<Expense>(`/projects/${projectId}/expenses/`, {
      method: "POST",
      body: payload,
    });
  },

  /** `GET /api/v1/expenses/{id}/` — detail. Requires `expense.view` scoped
   * to the expense's OWN project. */
  async getExpense(id: number): Promise<Expense> {
    return request<Expense>(`/expenses/${id}/`, { method: "GET" });
  },

  /** `PATCH /api/v1/expenses/{id}/` — requires `expense.manage` scoped to
   * the expense's own project. Partial update. */
  async updateExpense(id: number, payload: Partial<ExpenseWritePayload>): Promise<Expense> {
    return request<Expense>(`/expenses/${id}/`, { method: "PATCH", body: payload });
  },

  /** `DELETE /api/v1/expenses/{id}/` — requires `expense.manage` scoped to
   * the expense's own project, `204`. */
  async deleteExpense(id: number): Promise<void> {
    await request<void>(`/expenses/${id}/`, { method: "DELETE" });
  },

  /** `GET /api/v1/expense-categories/` — one page. Requires `project.view`.
   * Active-only by default (`?include_inactive=true` to include retired
   * ones — see `ExpenseCategoryListParams` doc comment). Used to populate
   * the Expense form's category `<Select>` (`ExpenseFormModal`) and to
   * resolve `Expense.category` (a bare id) to a name in the ledger
   * (`ExpensesTab`). */
  async listExpenseCategories(
    params?: ExpenseCategoryListParams,
  ): Promise<Paginated<ExpenseCategory>> {
    return request<Paginated<ExpenseCategory>>(`/expense-categories/${buildQuery(params)}`, {
      method: "GET",
    });
  },

  /** Walks every page of `/api/v1/expense-categories/` — bounded tenant
   * config (a handful of categories per tenant, same "walk every page"
   * reasoning as `listAllCategories`/`listAllLocations`/`listAllProjects`),
   * never an asset-scale list. */
  async listAllExpenseCategories(params?: ExpenseCategoryListParams): Promise<ExpenseCategory[]> {
    return fetchAllPages<ExpenseCategory>("/expense-categories/", params);
  },

  /** `GET /api/v1/expense-categories/{id}/` — detail (rarely needed
   * directly; `listAllExpenseCategories` covers the picker/resolution use
   * cases above), included for parity with the other resources' typed
   * client methods. */
  async getExpenseCategory(id: number): Promise<ExpenseCategory> {
    return request<ExpenseCategory>(`/expense-categories/${id}/`, { method: "GET" });
  },

  /** `GET /api/v1/expenses/{id}/attachment/` — list this expense's invoice
   * scans (plain array, NOT a `Paginated` envelope — see
   * `ExpenseAttachmentSerializer` usage in `apps.projects.api.
   * ExpenseViewSet.attachment`). Requires `expense.view` scoped to the
   * expense's project. */
  async listExpenseAttachments(expenseId: number): Promise<ExpenseAttachment[]> {
    return request<ExpenseAttachment[]>(`/expenses/${expenseId}/attachment/`, { method: "GET" });
  },

  /** `POST /api/v1/expenses/{id}/attachment/` — invoice/receipt scan upload
   * (multipart, same pattern as `uploadAssetAttachment`). Requires
   * `expense.manage` scoped to the expense's project. `kind` is
   * `"photo"`|`"doc"` (camera capture vs. a picked document/PDF). */
  async uploadExpenseAttachment(
    expenseId: number,
    file: File,
    kind: "photo" | "doc" = "photo",
  ): Promise<ExpenseAttachment> {
    const formData = new FormData();
    formData.append("file", file);
    formData.append("kind", kind);

    const headers = new Headers();
    const token = readCookie(CSRF_COOKIE_NAME);
    if (token) headers.set(CSRF_HEADER_NAME, token);

    const response = await fetch(`${API_BASE}/expenses/${expenseId}/attachment/`, {
      method: "POST",
      credentials: "include",
      headers,
      body: formData,
    });
    if (!response.ok) throw await toApiError(response);
    return (await response.json()) as ExpenseAttachment;
  },

  /** `DELETE /api/v1/expense-attachments/{id}/` — requires `expense.manage`
   * scoped to the attachment's own expense's project, `204`. Same pattern as
   * `deleteProjectDocument`. */
  async deleteExpenseAttachment(attachmentId: number): Promise<void> {
    await request<void>(`/expense-attachments/${attachmentId}/`, { method: "DELETE" });
  },

  /** `GET /api/v1/projects/{id}/documents/` — one page. **Gated by
   * `expense.view` scoped to this project, NOT `project.view`** (product
   * decision, `apps.projects.permissions._action_permission_key` doc
   * comment: proposals/contracts routinely restate the exact budget figures
   * redacted elsewhere) — a plain Member/Viewer or a Lead of a different
   * project gets a 403 on the WHOLE sub-resource here, not an empty list;
   * surface that as "you don't have access to this project's documents",
   * same posture as the financial redaction. */
  async listProjectDocuments(
    projectId: number,
    params?: ListParams,
  ): Promise<Paginated<ProjectDocument>> {
    return request<Paginated<ProjectDocument>>(
      `/projects/${projectId}/documents/${buildQuery(params)}`,
      { method: "GET" },
    );
  },

  /** `POST /api/v1/projects/{id}/documents/` — requires `project.manage`
   * scoped to this project. Multipart: `file` + `kind` (proposal/contract/
   * progress_report/other) + `file_kind` (`"photo"`|`"doc"`, defaults to
   * `"doc"` server-side — grant documents are virtually always PDFs/office
   * docs, not camera photos, but the picker still allows either). */
  async uploadProjectDocument(
    projectId: number,
    file: File,
    kind: ProjectDocumentKind,
    fileKind: "photo" | "doc" = "doc",
  ): Promise<ProjectDocument> {
    const formData = new FormData();
    formData.append("file", file);
    formData.append("kind", kind);
    formData.append("file_kind", fileKind);

    const headers = new Headers();
    const token = readCookie(CSRF_COOKIE_NAME);
    if (token) headers.set(CSRF_HEADER_NAME, token);

    const response = await fetch(`${API_BASE}/projects/${projectId}/documents/`, {
      method: "POST",
      credentials: "include",
      headers,
      body: formData,
    });
    if (!response.ok) throw await toApiError(response);
    return (await response.json()) as ProjectDocument;
  },

  /** `DELETE /api/v1/documents/{id}/` — requires `project.manage` scoped to
   * the document's own project, `204`. */
  async deleteProjectDocument(id: number): Promise<void> {
    await request<void>(`/documents/${id}/`, { method: "DELETE" });
  },

  /** `GET /api/v1/projects/{id}/export.csv/?fields=...` — streamed,
   * field-selectable export of this project's expense ledger. Requires
   * `expense.view` scoped to this project. Same "plain URL, browser
   * navigation, session cookie rides along" pattern as
   * `exportAssetsCsvUrl` — this only builds the URL, it never fetches the
   * body itself. */
  exportProjectCsvUrl(projectId: number, fields?: string[]): string {
    const query = fields && fields.length > 0 ? `?fields=${fields.join(",")}` : "";
    return `${API_BASE}/projects/${projectId}/export.csv/${query}`;
  },

  /** `POST /api/v1/projects/{id}/report/` — requires `expense.view` scoped
   * to this project (the report inlines the same redacted financial
   * figures, `apps.projects.permissions.PROJECT_ACTION_PERMISSION_MAP`
   * doc comment). Enqueues the PDF-render job and returns it immediately
   * (`202`, `queued`) — same job-poll contract as `generateLabels`: poll
   * `api.getJob(job.id)` until `status` lands on `succeeded`/`failed`.
   * `options.includeInvoiceScans` opts into embedding each invoice/receipt
   * image directly in the rendered PDF (bigger file); `options.
   * includeProjectDocuments` opts into appending the full pages of each
   * uploaded project document (proposals, contracts, progress reports,
   * other) onto the end of the PDF (can make it much larger). Both default
   * to `false`, matching the server's defaults when the fields are
   * omitted. */
  async generateProjectReport(
    projectId: number,
    options?: { includeInvoiceScans?: boolean; includeProjectDocuments?: boolean },
  ): Promise<Job> {
    return request<Job>(`/projects/${projectId}/report/`, {
      method: "POST",
      body: {
        include_invoice_scans: options?.includeInvoiceScans ?? false,
        include_project_documents: options?.includeProjectDocuments ?? false,
      },
    });
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

  /** `GET /api/v1/resolve/{qr_token}` — the scan/label target (T4.1,
   * `apps.assets.api.AssetResolveView`). `qr_token` is the stable per-asset
   * token embedded in the printed/scanned QR (`Asset.qr_token`); this is
   * also the manual-entry fallback's path (risk R5) when a user types/pastes
   * a token instead of scanning. Tenant-scoped: an unknown OR cross-tenant
   * token both 404 identically (R4: no existence leak) — same shape as
   * `getAsset`'s 404. NOTE: no trailing slash — a plain `path()` route, not
   * router-registered (matches `dashboard/summary`'s reasoning). */
  async resolveQrToken(token: string): Promise<Asset> {
    return request<Asset>(`/resolve/${encodeURIComponent(token)}`, { method: "GET" });
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

  /** `POST /api/v1/stock/` — creates a `StockItem` for a consumable asset
   * (post-MVP gap fill; requires `stock.adjust`, scoped to the asset's
   * project). `quantity_on_hand` always starts at `0` — see
   * `StockItemCreatePayload` doc comment for why: follow this with
   * `postStockTxn(..., {reason: "receive", delta: <n>})` to set an actual
   * starting count, presented to the user as one "set up stock" step even
   * though it's two requests. A `400` under `errors.asset` means the asset
   * either isn't consumable or already has a `StockItem` — surface it inline,
   * same "server is the authority" pattern as everywhere else in this module. */
  async createStockItem(payload: StockItemCreatePayload): Promise<StockItem> {
    return request<StockItem>("/stock/", { method: "POST", body: payload });
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
   * `?overdue=true` are the documented filters (T3.5 My Items screen);
   * `?asset=`/`?reservation=` (post-MVP gap fill) let a caller reliably find
   * "the (open) checkout for this specific asset/reservation" instead of
   * scanning a bounded open-checkouts page client-side. */
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

  // --- Dashboard (docs/api-and-ui.md "Maintenance, labels, import/export,
  // dashboard"; apps.dashboard.api.DashboardSummaryView) ---
  // NOTE: no trailing slash — a plain `path()` route, not a router-registered
  // viewset (see `DashboardSummary` type doc comment / `apps.dashboard.api`
  // module docstring), so the `APPEND_SLASH` reasoning above doesn't apply.

  /** `GET /api/v1/dashboard/summary` — requires `asset.view` in any scope;
   * every tile is already scoped server-side to the caller's viewable
   * projects. Cached server-side (30s TTL) — call fresh on every screen
   * mount, no client-side caching needed. */
  async getDashboardSummary(): Promise<DashboardSummary> {
    return request<DashboardSummary>("/dashboard/summary", { method: "GET" });
  },

  // --- Labels / jobs (T4.5; `apps.labels.api.LabelGenerateView`/
  // `apps.jobs.api.JobRetrieveView`) --- NOTE: no trailing slash — plain
  // `path()` routes, not router-registered viewsets, same reasoning as
  // `dashboard/summary` above.

  /** `POST /api/v1/labels/generate` — requires `label.generate` (scoped;
   * Admin tenant-wide, ProjectLead within their own project's assets).
   * Returns immediately (`202`) with a `queued` `Job` — poll `getJob` until
   * `status` is `succeeded`/`failed`. Any requested asset id outside the
   * caller's tenant/scope is silently dropped server-side; if that leaves
   * none, the request 400s (surfaced as a normal `ApiError`). */
  async generateLabels(payload: LabelGenerateRequest): Promise<Job> {
    return request<Job>("/labels/generate", { method: "POST", body: payload });
  },

  /** `GET /api/v1/jobs/{id}` — the caller must be the job's own creator
   * (server-enforced, `apps.jobs.api.JobRetrieveView`); anyone else's job id
   * 404s. Poll on an interval until `status` is `succeeded`/`failed`. */
  async getJob(id: string): Promise<Job> {
    return request<Job>(`/jobs/${id}`, { method: "GET" });
  },

  // --- Bulk import / export (T6.2; `apps.imports.api`/`apps.imports.exports`) ---
  // NOTE: no trailing slash — plain `path()` routes, same reasoning as
  // `dashboard/summary`/`labels/generate` above.

  /** `POST /api/v1/imports` — requires `import.run` (Admin, tenant-wide).
   * Multipart (like `uploadAssetAttachment`): the file plus an optional
   * JSON-encoded `mapping` override (`{header: target}`; omitted headers
   * fall back to the server's auto-detected default). Kicks off a dry-run
   * and returns immediately (`202`) with the new `ImportJob` — poll
   * `getImport` until its `status` lands on `dry_run_succeeded`/
   * `dry_run_failed`. */
  async createImport(file: File, mapping?: ImportMapping): Promise<ImportJob> {
    const formData = new FormData();
    formData.append("file", file);
    if (mapping && Object.keys(mapping).length > 0) {
      formData.append("mapping", JSON.stringify(mapping));
    }

    const headers = new Headers();
    const token = readCookie(CSRF_COOKIE_NAME);
    if (token) headers.set(CSRF_HEADER_NAME, token);

    const response = await fetch(`${API_BASE}/imports`, {
      method: "POST",
      credentials: "include",
      headers,
      body: formData,
    });
    if (!response.ok) throw await toApiError(response);
    return (await response.json()) as ImportJob;
  },

  /** `GET /api/v1/imports/{id}` — requires `import.run`, not narrowed to
   * `created_by=request.user` (any tenant Admin may look up/commit a
   * colleague's import). Poll on an interval while `status` is `pending`/
   * `dry_run_running`/`committing`. */
  async getImport(id: number): Promise<ImportJob> {
    return request<ImportJob>(`/imports/${id}`, { method: "GET" });
  },

  /** `POST /api/v1/imports/{id}/commit` — requires `import.run`. `409`s
   * (surfaced as a normal `ApiError`) unless the import's last dry-run/
   * commit attempt succeeded/failed-validation (`dry_run_succeeded` or
   * `commit_failed`) — i.e. you can't commit an import that has never had a
   * successful dry-run. `mapping` is optional: omitted re-uses the
   * already-confirmed mapping from the last dry-run. All-or-nothing
   * server-side: if the re-validation at commit time finds ANY invalid row,
   * nothing is created and the job lands on `commit_failed` with the same
   * per-row report. */
  async commitImport(id: number, mapping?: ImportMapping): Promise<ImportJob> {
    return request<ImportJob>(`/imports/${id}/commit`, {
      method: "POST",
      body: mapping && Object.keys(mapping).length > 0 ? { mapping } : {},
    });
  },

  /** `GET /api/v1/exports/assets.csv` — requires `asset.export`, honors the
   * SAME query params as `listAssets` (search/ordering/category/location/
   * project/tag/status/is_consumable/include_retired). Not a `fetch()`
   * helper: this is a plain streamed-file `GET`, so the simplest correct
   * client is a browser navigation/`<a href>` (session cookie rides along
   * automatically) — this just builds that URL, it never fetches the body
   * itself. */
  exportAssetsCsvUrl(params?: AssetListParams): string {
    return `${API_BASE}/exports/assets.csv${buildQuery(params)}`;
  },

  // --- Notification preferences (docs/api-and-ui.md "Per-user prefs";
  // apps.notifications.api.NotificationPrefViewSet) ---
  // Trailing slashes, same `APPEND_SLASH` reason as Categories/Locations/Stock.

  /** `GET /api/v1/notification-prefs/` — the caller's OWN rows only
   * (`get_queryset` filters to `request.user`); requires `notify.self` (every
   * role). Paginated envelope per the default `PageNumberPagination`, though
   * in practice there are only a handful of known event types. */
  async listNotificationPrefs(): Promise<Paginated<NotificationPref>> {
    return request<Paginated<NotificationPref>>("/notification-prefs/", { method: "GET" });
  },

  /** `PATCH /api/v1/notification-prefs/{event_type}/` — upserts: a user with
   * no explicit row yet for `event_type` gets one created here (defaulting
   * from enabled), rather than 404ing (`apps.notifications.api` module
   * docstring). `event_type` is the lookup key, not a numeric id. */
  async updateNotificationPref(
    eventType: string,
    payload: NotificationPrefUpdatePayload,
  ): Promise<NotificationPref> {
    return request<NotificationPref>(`/notification-prefs/${eventType}/`, {
      method: "PATCH",
      body: payload,
    });
  },

  // --- Email settings (docs/api-and-ui.md; apps.notifications.api.
  // EmailSettingsView) --- NOTE: no trailing slash — this is a plain
  // `path()`-registered singleton, not a router-registered ModelViewSet
  // collection (see `backend/config/urls.py`'s `email-settings` route).

  /** `GET /api/v1/notifications/email-settings` — requires `tenant.manage`
   * (BOTH read and write are gated on it server-side; a non-admin 403s on
   * this GET too, not just on writes). */
  async getEmailSettings(): Promise<EmailSettings> {
    return request<EmailSettings>("/notifications/email-settings", { method: "GET" });
  },

  /** `PATCH /api/v1/notifications/email-settings` — requires `tenant.manage`.
   * `payload.api_key` has "omit vs blank" semantics — see
   * `EmailSettingsUpdate` doc comment; only include the key when the caller
   * actually means to change/clear it. */
  async updateEmailSettings(payload: EmailSettingsUpdate): Promise<EmailSettings> {
    return request<EmailSettings>("/notifications/email-settings", {
      method: "PATCH",
      body: payload,
    });
  },

  /** `POST /api/v1/notifications/email-settings/test` — requires
   * `tenant.manage`. No request body; the server always sends to the caller's
   * own email using whatever is already saved for the tenant (not unsaved
   * form values). Rejects with a 400 problem+json (e.g. no API key or no
   * sender email configured) — same `ApiError` handling as every other
   * write in this module. */
  async sendTestEmail(): Promise<EmailSettingsTestResult> {
    return request<EmailSettingsTestResult>("/notifications/email-settings/test", {
      method: "POST",
    });
  },

  // --- Audit log (docs/api-and-ui.md "Audit log (scoped)";
  // apps.audit.api.AuditLogViewSet) ---
  // Trailing slash, same `APPEND_SLASH` reason as above. Read-only — no
  // create/update/destroy call exists (append-only log).

  /** `GET /api/v1/audit/` — one page. Requires `audit.view`: tenant-wide for
   * Admin, own-project-scoped only for a ProjectLead (server-enforced,
   * `apps.audit.api.AuditLogViewSet.get_queryset`) — anyone else gets a 403,
   * handled as a normal outcome (CLAUDE.md), never assumed to be a bug.
   * Never walks all pages (CLAUDE.md: server-side lists). */
  async listAuditLog(params?: AuditLogListParams): Promise<Paginated<AuditLogEntry>> {
    return request<Paginated<AuditLogEntry>>(`/audit/${buildQuery(params)}`, { method: "GET" });
  },

  // --- Users & Roles admin (Users & Roles screen; apps.accounts.api.UserViewSet,
  // apps.rbac.api.MembershipViewSet/RoleViewSet) ---
  // Trailing slash, same `APPEND_SLASH` reason as Categories/Locations above.

  /** `GET /api/v1/users/` — one page, `?search=` on email/name. Any
   * `user.manage` scope may list (server-side, `apps.accounts.permissions.
   * UserManagementPermission`). Used for the "add member" user picker's
   * search-as-you-type and never walked across every page (CLAUDE.md). */
  async listUsers(params?: ListParams): Promise<Paginated<AppUser>> {
    return request<Paginated<AppUser>>(`/users/${buildQuery(params)}`, { method: "GET" });
  },

  /** `POST /api/v1/users/` — Admin-only (tenant-wide `user.manage`).
   * Response includes a ONE-TIME `password` field (the generated initial
   * password) — see `CreatedUser` doc comment for the handling
   * requirements; never persist it beyond the one-time-reveal modal. */
  async createUser(payload: CreateUserPayload): Promise<CreatedUser> {
    return request<CreatedUser>("/users/", { method: "POST", body: payload });
  },

  /** `POST /api/v1/users/{id}/reset-password/` — Admin-only (tenant-wide
   * `user.manage`). Regenerates the user's password and returns a ONE-TIME
   * `password` field (same handling as `createUser` — reveal once, never
   * persist). Trailing slash: it's a router `@action`. */
  async resetUserPassword(userId: number): Promise<CreatedUser> {
    return request<CreatedUser>(`/users/${userId}/reset-password/`, { method: "POST" });
  },

  /** `GET /api/v1/roles/` — the tenant's 4 seeded system roles. Requires
   * any `user.manage` scope. Small, bounded list — not paginated by the
   * caller (one page always covers it), but the envelope is still the
   * standard `Paginated<T>` shape. */
  async listRoles(): Promise<Paginated<Role>> {
    return request<Paginated<Role>>("/roles/", { method: "GET" });
  },

  /** `GET /api/v1/memberships/` — one page. An Admin (tenant-wide
   * `user.manage`) sees every tenant Membership; a ProjectLead sees only
   * their own project's (server-enforced, `apps.rbac.api.
   * MembershipViewSet.get_queryset`). */
  async listMemberships(params?: MembershipListParams): Promise<Paginated<Membership>> {
    return request<Paginated<Membership>>(`/memberships/${buildQuery(params)}`, { method: "GET" });
  },

  /** `POST /api/v1/memberships/` — grants `role` to `user`, scoped to
   * `project` (omitted/`null` = tenant-wide). Admin may grant any role/
   * project; a ProjectLead is further restricted server-side (`apps.rbac.
   * permissions.MembershipPermission`) — this client method itself applies
   * no extra restriction, the server is the authority. */
  async createMembership(payload: MembershipCreatePayload): Promise<Membership> {
    return request<Membership>("/memberships/", { method: "POST", body: payload });
  },

  /** `PATCH /api/v1/memberships/{id}/` — role change only; `user`/`project`
   * are read-only past creation (see `MembershipUpdatePayload` doc
   * comment). */
  async updateMembership(id: number, payload: MembershipUpdatePayload): Promise<Membership> {
    return request<Membership>(`/memberships/${id}/`, { method: "PATCH", body: payload });
  },

  /** `DELETE /api/v1/memberships/{id}/` — removes the membership (a real
   * permission revocation), `204`. */
  async deleteMembership(id: number): Promise<void> {
    await request<void>(`/memberships/${id}/`, { method: "DELETE" });
  },
};

export { ApiError };
