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
  /** `/media/<key>` URL of the lab's uploaded logo, or `null` when none is
   * set (the UI falls back to the lab's initials). Set from Admin ->
   * Lab Branding (`POST /api/v1/tenancy/logo`). */
  logo_url: string | null;
}

/** `GET/POST/DELETE /api/v1/tenancy/logo` response — the lab's branding as
 * the UI shows it. */
export interface TenantBranding {
  id: number;
  slug: string;
  name: string;
  logo_url: string | null;
  logo_filename: string;
  logo_updated_at: string | null;
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
  /** The RAW stored name — may be `""` for a user who has never set one.
   * Use `display_name` for anything rendered; this field exists so the
   * Account form round-trips exactly what is stored. */
  name: string;
  /** What to render: `name` when set, otherwise the email. */
  display_name: string;
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

/** `PATCH /api/v1/me` request body — self-service profile edit. `name` is
 * the only self-settable field (`apps.accounts.serializers.UpdateMeSerializer`
 * is an explicit allowlist); the response is the full `Me` shape. */
export interface UpdateMeRequest {
  name: string;
}

/** `POST /api/v1/me/password` request body (`apps.accounts.serializers.
 * ChangePasswordSerializer`). Self-service password change. */
export interface ChangePasswordRequest {
  current_password: string;
  new_password: string;
}

/** `POST /api/v1/auth/password-reset/request` body — `tenant` slug
 * disambiguates the per-tenant-unique email, same as `LoginRequest`. */
export interface ForgotPasswordRequest {
  tenant: string;
  email: string;
}

/** `POST /api/v1/auth/password-reset/confirm` body. `tenant` + one-time
 * `token` (both carried in the emailed reset link) plus the chosen password. */
export interface PasswordResetConfirmRequest {
  tenant: string;
  token: string;
  new_password: string;
}

/**
 * DRF `PageNumberPagination` envelope — every list endpoint returns this
 * shape (`docs/api-and-ui.md` §1: "every list endpoint is paginated").
 * `count`/`next`/`previous` let the client walk multi-page result sets
 * without ever assuming a single page is "everything".
 */
export interface Paginated<T> {
  count: number;
  next: string | null;
  previous: string | null;
  results: T[];
}

/**
 * `backend/apps/catalog/models.py::CustomFieldDef.DataType`. Drives the
 * category-driven dynamic asset form (T1.7) and the admin field-def editor
 * (T1.5) alike — keep in lockstep with the backend choices.
 */
export type CustomFieldDataType =
  | "text"
  | "int"
  | "float"
  | "bool"
  | "date"
  | "enum"
  | "json"
  | "url";

/** `GET /api/v1/categories/{id}/fields` item shape
 * (`apps.catalog.serializers.CustomFieldDefSerializer`). `category` is
 * always present on read but server-derived (never client-writable — see
 * `CustomFieldDefCreatePayload` below, which omits it). */
export interface CustomFieldDef {
  id: number;
  category: number;
  key: string;
  label: string;
  data_type: CustomFieldDataType;
  unit: string;
  enum_options: string[];
  required: boolean;
  order: number;
  created_at: string;
}

/** `POST /api/v1/categories/{id}/fields` request body. `category` is derived
 * from the URL server-side (`apps.catalog.api.CategoryViewSet.fields`) —
 * never send it in the body. */
export interface CustomFieldDefCreatePayload {
  key: string;
  label: string;
  data_type: CustomFieldDataType;
  unit?: string;
  enum_options?: string[];
  required?: boolean;
  order?: number;
}

/** `PATCH /api/v1/categories/{cat_id}/fields/{field_id}` request body
 * (M1 follow-up, `docs/api-and-ui.md` "Custom field def edit/delete/reorder").
 * Partial update — every key optional. `category`/`tenant` are never
 * client-writable (derived from the URL/session), same as create.
 * **Policy:** changing `data_type` once the field has one or more stored
 * `AssetFieldValue` rows is rejected with a `400` (`errors.data_type`) —
 * every other attribute stays freely editable at any time. `(category, key)`
 * uniqueness is re-checked the same way as on create (`400` on `errors.key`,
 * never a raw `IntegrityError`/500). */
export interface CustomFieldDefUpdatePayload {
  key?: string;
  label?: string;
  data_type?: CustomFieldDataType;
  unit?: string;
  enum_options?: string[];
  required?: boolean;
  order?: number;
}

/** `GET/POST/PATCH /api/v1/categories` (`apps.catalog.serializers.CategorySerializer`). */
export interface Category {
  id: number;
  name: string;
  parent: number | null;
  default_is_consumable: boolean;
  requires_approval: boolean;
  requires_calibration: boolean;
  field_defs: CustomFieldDef[];
  created_at: string;
}

/** `POST`/`PATCH /api/v1/categories` request body. */
export interface CategoryWritePayload {
  name: string;
  parent?: number | null;
  default_is_consumable?: boolean;
  requires_approval?: boolean;
  requires_calibration?: boolean;
}

/** `GET/POST/PATCH /api/v1/locations` (`apps.catalog.serializers.LocationSerializer`). */
export interface Location {
  id: number;
  name: string;
  parent: number | null;
  kind: string;
  created_at: string;
}

/** `POST`/`PATCH /api/v1/locations` request body. */
export interface LocationWritePayload {
  name: string;
  parent?: number | null;
  kind?: string;
}

/** Common list query params supported by the catalog list endpoints
 * (`docs/api-and-ui.md` §1: every list endpoint supports `?search=`,
 * `?ordering=`, and field filters; `apps.catalog.api` `filterset_fields`). */
export interface ListParams {
  search?: string;
  ordering?: string;
  page?: number;
  [key: string]: string | number | boolean | null | undefined;
}

/** `Project.FundingSource`/`Project.Status` choices
 * (`backend/apps/projects/models.py::Project`). `""` is a valid `funding_source`
 * value (unset — pre-M7 projects/plain asset-grouping lenses never had one). */
export type ProjectFundingSource = "internal" | "external" | "";
export type ProjectStatus = "active" | "closed";

/** `GET/POST/PATCH /api/v1/projects` (`apps.projects.serializers.
 * ProjectSerializer`, registered as `ProjectViewSet` in `apps.projects.api` —
 * M7 supersedes the earlier thin `apps.catalog.api.ProjectViewSet`, same
 * route/contract, see that module's own "Route ownership" docstring).
 *
 * **Financial redaction (product decision, `docs/tasks/M7-project-grants.md`
 * "Product decision"):** `budget_total` is `null` on the wire for any caller
 * who does not hold `expense.view` scoped to THIS project (a Member with no
 * project-scoped Lead grant, or a Lead of a different project) — render that
 * as a locked/"—" affordance, NEVER as `0`/`$0.00`. This applies to every
 * list row too, not just the detail view (`ProjectSerializer.
 * to_representation`), so the Projects list must handle it the same way. */
export interface Project {
  id: number;
  name: string;
  code: string;
  lead_user: number | null;
  is_active: boolean;
  funding_source: ProjectFundingSource;
  sponsor: string;
  start_date: string | null;
  end_date: string | null;
  /** Redacted to `null` unless the caller holds `expense.view` scoped to
   * this project — see interface doc comment above. A real awarded budget
   * of exactly `"0.00"` is a valid (if unusual) string; `null` is the ONLY
   * "hidden" sentinel — never conflate the two in the UI. */
  budget_total: string | null;
  currency: string;
  status: ProjectStatus;
  description: string;
  created_at: string;
}

/** `GET/PATCH /api/v1/projects/{id}` — the M7 project hub detail
 * (`apps.projects.serializers.ProjectDetailSerializer`), adding the computed
 * budget rollup. `spent`/`remaining`/`spend_by_category` are redacted to
 * `null` under the EXACT SAME per-project `expense.view` gate as
 * `budget_total` above (same doc comment applies: render as a locked
 * affordance, never `$0`/an empty breakdown). */
export interface ProjectDetail extends Project {
  spent: string | null;
  remaining: string | null;
  spend_by_category: SpendByCategoryRow[] | null;
}

/** One row of `ProjectDetail.spend_by_category`
 * (`apps.projects.serializers.ProjectDetailSerializer.get_spend_by_category`).
 * `category_id`/`category` are `null` for expenses with no category
 * (`Expense.category` is `SET_NULL`). */
export interface SpendByCategoryRow {
  category_id: number | null;
  category: string | null;
  total: string;
}

/** `GET /api/v1/projects` query params (`apps.projects.api.ProjectViewSet`:
 * `filterset_fields = ["is_active", "status"]`, `search_fields = ["name",
 * "code"]`, `ordering_fields = ["name", "created_at"]`). */
export interface ProjectListParams extends ListParams {
  is_active?: boolean;
  status?: ProjectStatus;
  ordering?: "name" | "-name" | "created_at" | "-created_at";
}

/** `PATCH /api/v1/projects/{id}` request body (`project.manage`-gated grant
 * metadata edit, `apps.projects.api.ProjectViewSet._PROJECT_AUDIT_FIELDS`) —
 * every field optional (partial update). `create`/`POST /api/v1/projects`
 * (Admin-only `tenant.manage`) still uses the narrower legacy
 * `{name, lead_user?, is_active?}` shape via `api.createProject` — the M7
 * grant fields can be filled in immediately after via this same PATCH path,
 * kept as a separate type since create's contract is deliberately unchanged
 * (`apps.projects.api` module docstring: "create/destroy... deliberately
 * IDENTICAL to the superseded viewset's contract"). */
/** `POST /api/v1/projects` request body — `apps.projects.serializers.
 * ProjectSerializer` is used for BOTH create/list and is the base class
 * `ProjectDetailSerializer` extends, so the full M7 grant-metadata field set
 * (`code`/`funding_source`/`sponsor`/dates/`budget_total`/`currency`/
 * `status`/`description`) is writable on create too, not just `name`/
 * `lead_user`/`is_active` (the pre-M7 admin CRUD screen's narrower usage,
 * `apps.projects.api` module docstring: "create... deliberately IDENTICAL to
 * the superseded viewset's contract" refers to the gating/validation
 * behavior, not a narrower field set). `create`/`destroy` stay
 * `tenant.manage`-gated (Admin-only) regardless of which fields are filled
 * in at creation time. */
export interface ProjectCreatePayload {
  name: string;
  code?: string;
  lead_user?: number | null;
  is_active?: boolean;
  funding_source?: ProjectFundingSource;
  sponsor?: string;
  start_date?: string | null;
  end_date?: string | null;
  budget_total?: string | null;
  currency?: string;
  status?: ProjectStatus;
  description?: string;
}

export interface ProjectUpdatePayload {
  name?: string;
  code?: string;
  lead_user?: number | null;
  is_active?: boolean;
  funding_source?: ProjectFundingSource;
  sponsor?: string;
  start_date?: string | null;
  end_date?: string | null;
  budget_total?: string | null;
  currency?: string;
  status?: ProjectStatus;
  description?: string;
}

// --- M7 project hub: expenses, invoice attachments, documents
// (`apps.projects.api`/`apps.projects.serializers`) ---

/** `GET /expenses/{id}/attachment` / `POST` response item
 * (`apps.projects.serializers.ExpenseAttachmentSerializer`) — an invoice/
 * receipt scan. Same storage-key convention as the asset `Attachment` type,
 * but no `kind` field (every row here is implicitly an invoice scan) and
 * anchors to an `Expense`, not an `Asset` — see `apps.projects.models.
 * ExpenseAttachment` doc comment for why this is a dedicated model/type
 * rather than the reused asset `Attachment` shape. */
export interface ExpenseAttachment {
  id: number;
  storage_key: string;
  filename: string;
  content_type: string;
  size: number;
  uploaded_by: number | null;
  created_at: string;
}

/** `GET /projects/{id}/expenses` (list row) / `GET /expenses/{id}` (detail) —
 * both use `apps.projects.serializers.ExpenseSerializer`. `category`/`asset`
 * are plain ids (not nested) — same "id, not nested" convention as
 * `Reservation.asset`/`StockItem.asset`.
 *
 * **Known gap (flagged for backend-engineer):** there is no
 * `GET /api/v1/expense-categories` (or similar) endpoint in this slice
 * (`apps.projects.tests.test_project_hub_tenant_isolation.
 * TestCrossTenantExpenseCategoryReference` docstring: "`ExpenseCategory` has
 * no dedicated CRUD endpoint in this slice") — the only way a category is
 * reachable from the client is by numeric id, on `Expense.category` itself.
 * Until that endpoint exists, the Expense form/ledger can only show/accept a
 * category **id**, not its name (see `ExpensesTab`'s own comment for the
 * workaround this forces). */
export interface Expense {
  id: number;
  project: number;
  category: number | null;
  amount: string;
  currency: string;
  date: string;
  vendor: string;
  invoice_number: string;
  description: string;
  asset: number | null;
  created_by: number | null;
  attachments: ExpenseAttachment[];
  created_at: string;
  updated_at: string;
}

/** `POST /projects/{id}/expenses` / `PATCH /expenses/{id}` request body.
 * `project`/`created_by` are server-derived (URL/session) — never
 * client-writable (`apps.projects.serializers.ExpenseSerializer.Meta.
 * read_only_fields`). */
export interface ExpenseWritePayload {
  category?: number | null;
  amount: string;
  currency?: string;
  date: string;
  vendor?: string;
  invoice_number?: string;
  description?: string;
  asset?: number | null;
}

/** `GET /projects/{id}/expenses` query params (`apps.projects.api.
 * ExpenseFilterSet`). `project` itself is fixed by the URL, not a filter. */
export interface ExpenseListParams extends ListParams {
  category?: number;
  date_from?: string;
  date_to?: string;
}

/** `GET /api/v1/expense-categories` (list row) / `GET /api/v1/
 * expense-categories/{id}` (detail) — a small follow-up endpoint added after
 * the M7 frontend slice flagged its absence (`apps.projects.models.
 * ExpenseCategory`: tenant-wide config, "Equipment"/"Consumables"/etc.).
 * Read-only, gated by `project.view`; ordered by `name`; active-only by
 * default (`?include_inactive=true` to include retired categories — see
 * `ExpenseCategoryListParams`). This is what lets `ExpenseFormModal`/
 * `ExpensesTab` show a real name instead of a bare `Expense.category` id. */
export interface ExpenseCategory {
  id: number;
  name: string;
  is_active: boolean;
}

/** `GET /api/v1/expense-categories` query params. `include_inactive`
 * defaults to `false` server-side (active-only) — only set it `true` when a
 * caller specifically needs to resolve a retired category's name (e.g. an
 * older expense still referencing one via `SET_NULL`-safe FK). */
export interface ExpenseCategoryListParams extends ListParams {
  include_inactive?: boolean;
}

/** `ProjectDocument.Kind` choices (`backend/apps/projects/models.py::
 * ProjectDocument.Kind`). */
export type ProjectDocumentKind = "proposal" | "contract" | "progress_report" | "other";

/** `GET/POST /projects/{id}/documents` row shape (`apps.projects.
 * serializers.ProjectDocumentSerializer`) — read-only wire shape; creation is
 * always via the dedicated multipart upload action (see `api.
 * uploadProjectDocument`), same pattern as asset `Attachment`. */
export interface ProjectDocument {
  id: number;
  project: number;
  kind: ProjectDocumentKind;
  storage_key: string;
  filename: string;
  content_type: string;
  size: number;
  uploaded_by: number | null;
  created_at: string;
}

/** `GET /api/v1/tags` (`apps.catalog.serializers.TagSerializer`) — read-only:
 * tags are created inline when tagging an asset, never through their own
 * write endpoint. */
export interface Tag {
  id: number;
  name: string;
  created_at: string;
}

/** `Asset.Status` choices (`backend/apps/assets/models.py::Asset.Status`). */
export type AssetStatus =
  | "available"
  | "in_use"
  | "reserved"
  | "maintenance"
  | "retired"
  | "lost";

/** `GET /api/v1/assets/{id}/attachments` item shape
 * (`apps.assets.serializers.AttachmentSerializer`) — read-only, embedded on
 * the asset detail/list row; created via the dedicated multipart upload
 * action, never through the asset serializer's own create/update. */
/** Semantic label for an attachment (`apps.assets.models.Attachment.DocType`).
 * Deliberately orthogonal to `kind`: `kind` is the storage/content-type shape
 * (image vs document), `doc_type` is what the file actually IS. A phone photo
 * of a paper invoice is `kind: "photo"` AND `doc_type: "invoice"`. `""` means
 * untagged (legacy rows, or nobody classified it). */
export type AttachmentDocType =
  | ""
  | "invoice"
  | "purchase_order"
  | "receipt"
  | "quote"
  | "warranty"
  | "manual"
  | "other";

export interface Attachment {
  id: number;
  kind: "photo" | "doc";
  doc_type: AttachmentDocType;
  storage_key: string;
  filename: string;
  content_type: string;
  size: number;
  uploaded_by: number | null;
  created_at: string;
}

/** `GET /api/v1/assets` (list row) / `GET /api/v1/assets/{id}` (detail) —
 * both use the SAME serializer (`apps.assets.serializers.AssetSerializer`),
 * so the list row and the detail record share this one shape (docs/api-and-
 * ui.md's Assets table only distinguishes them by URL, not by response
 * shape). `field_values` is `{ [CustomFieldDef.key]: <already-typed value> }`
 * — int/float as `number`, `bool` as `boolean`, `date` as an ISO date
 * string, `text`/`enum` as `string`, `json` as whatever was stored — keyed
 * by field def `key`, NOT indexed by `CustomFieldDef.id` (see
 * `AssetSerializer.get_field_values`). Rendering it against the category's
 * `CustomFieldDef[]` (for `label`/`data_type`/`unit`/`order`) is the
 * caller's job — this type alone doesn't carry the schema. */
export interface Asset {
  id: number;
  uuid: string;
  qr_token: string;
  category: number;
  name: string;
  description: string;
  is_consumable: boolean;
  project: number | null;
  serial_number: string;
  manufacturer: string;
  model: string;
  location: number | null;
  purchase_date: string | null;
  purchase_cost: string | null;
  currency: string;
  warranty_expiry: string | null;
  supplier: string;
  /** Built-in link (product/procurement page). `""` when unset; the server
   * only ever stores an http/https URL
   * (`AssetSerializer.validate_url`). */
  url: string;
  status: AssetStatus;
  condition: string;
  retired_at: string | null;
  current_workload_user: number | null;
  tags: string[];
  field_values: Record<string, unknown>;
  attachments: Attachment[];
  created_at: string;
  updated_at: string;
}

/**
 * `POST /api/v1/assets` (create) / `PATCH /api/v1/assets/{id}` (edit) request
 * body (`apps.assets.serializers.AssetSerializer`, T1.7). Every field is
 * optional on `PATCH` (partial update); `category` and `name` are the only
 * ones the server actually requires on `POST` (create) — see
 * `AssetFormScreen` for the client-side mirror of that. `custom_field_values`
 * is a `{ [CustomFieldDef.key]: rawValue }` dict, validated/coerced
 * server-side against the target category's `CustomFieldDef` list
 * (`apps.assets.services.validate_custom_field_values`) — sending it (even as
 * `{}`) on an edit re-validates the FULL required set for the category
 * (`AssetSerializer.update`'s "supplied vs. omitted" distinction), which is
 * why the form always includes the key once a category is selected.
 */
export interface AssetWritePayload {
  category?: number;
  name?: string;
  description?: string;
  is_consumable?: boolean;
  project?: number | null;
  serial_number?: string;
  manufacturer?: string;
  model?: string;
  location?: number | null;
  purchase_date?: string | null;
  purchase_cost?: string | null;
  currency?: string;
  warranty_expiry?: string | null;
  supplier?: string;
  url?: string;
  status?: AssetStatus;
  condition?: string;
  tags?: string[];
  custom_field_values?: Record<string, unknown>;
}

/** `?ordering=` whitelist (`apps.assets.api.AssetViewSet.ordering_fields`) —
 * any other value is silently ignored server-side (DRF `OrderingFilter`),
 * never a 400, but the typed client only ever offers these. */
export type AssetOrdering =
  | "name"
  | "-name"
  | "created_at"
  | "-created_at"
  | "status"
  | "-status"
  | "purchase_date"
  | "-purchase_date";

/** `GET /api/v1/assets` query params (`docs/api-and-ui.md` Assets table;
 * `apps.assets.api.AssetFilterSet`/`AssetViewSet`). Every FK filter is a
 * plain id-equality match — never build these ad hoc in a component
 * (CLAUDE.md), always go through `api.listAssets`. `cursor` opts into
 * `AssetCursorPagination` server-side instead of the default bounded
 * `?page`/`?page_size` mode (mutually exclusive with `page` in practice,
 * though the server tolerates both being present by preferring cursor mode). */
export interface AssetListParams extends ListParams {
  ordering?: AssetOrdering;
  page_size?: number;
  cursor?: string;
  category?: number;
  status?: AssetStatus;
  location?: number;
  project?: number;
  tag?: number;
  is_consumable?: boolean;
  /** Retired assets are hidden from the default list server-side; this is
   * the documented opt-in to include them (`apps.assets.api.AssetViewSet.
   * get_queryset`). */
  include_retired?: boolean;
}

// --- Stock / consumables (T2.4; `apps.stock.serializers`/`apps.stock.api`) ---
// `docs/api-and-ui.md` "Stock" table: `GET /stock` (paginated, `?low_stock=`),
// `POST /stock/{id}/txn` (ledger-backed), `GET/POST/PATCH /reorder-requests`.

/** `StockTxn.Reason` choices (`backend/apps/stock/models.py::StockTxn.Reason`). */
export type StockTxnReason = "receive" | "consume" | "adjust" | "correction";

/** `ReorderRequest.Status` choices (`backend/apps/stock/models.py::
 * ReorderRequest.Status`) — valid forward transitions are
 * open -> approved -> ordered -> received, with cancelled reachable from any
 * non-terminal state (server-enforced; see `ReorderRequest.VALID_TRANSITIONS`). */
export type ReorderRequestStatus = "open" | "approved" | "ordered" | "received" | "cancelled";

/** `GET /api/v1/stock` (list row) / `GET /api/v1/stock/{id}` (detail) —
 * `apps.stock.serializers.StockItemSerializer`. `asset` is a plain id (not
 * nested) — the screen resolves the asset's name/project separately via
 * `api.getAsset`. `quantity_on_hand` is server-derived/reconciled against the
 * `StockTxn` ledger — never client-writable. */
export interface StockItem {
  id: number;
  asset: number;
  unit_of_measure: string;
  quantity_on_hand: number;
  reorder_threshold: number;
  reorder_target: number;
  bin_location: number | null;
  created_at: string;
  updated_at: string;
}

/** `POST /api/v1/stock/` request body (post-MVP gap fill, `apps.stock.api`
 * module docstring / `apps.stock.serializers.StockItemSerializer`). Requires
 * `stock.adjust`; `asset` must be `is_consumable` and not already own a
 * `StockItem` (both re-validated server-side — a `400` under `errors.asset`
 * either way). `quantity_on_hand` is NOT settable here (always starts at the
 * model default, `0`) — follow up with `api.postStockTxn(..., {reason:
 * "receive", delta: <n>})` to set an actual initial count in a second call. */
export interface StockItemCreatePayload {
  asset: number;
  unit_of_measure: string;
  reorder_threshold?: number;
  reorder_target?: number;
  bin_location?: number | null;
}

/** `GET /api/v1/stock` query params (`apps.stock.api.StockItemViewSet`).
 * `low_stock=true` filters to `quantity_on_hand <= reorder_threshold`
 * (the T2.3 partial-index scan). `ordering` whitelist matches
 * `StockItemViewSet.ordering_fields`. **`search` is inherited from
 * `ListParams` but NOT implemented server-side** — `StockItemViewSet` only
 * wires `DjangoFilterBackend`/`OrderingFilter` (no `SearchFilter`), and
 * `docs/api-and-ui.md`'s Stock endpoint documents only the low-stock filter
 * (code-review finding, T2.4). Do not send `search` here until a backend
 * task adds it; `StockScreen` deliberately has no search input. */
export interface StockListParams extends ListParams {
  low_stock?: boolean;
  ordering?: "quantity_on_hand" | "-quantity_on_hand" | "reorder_threshold" | "-reorder_threshold" | "created_at" | "-created_at";
}

/** Ledger row appended by `POST /api/v1/stock/{id}/txn`
 * (`apps.stock.serializers.StockTxnSerializer`) — append-only, never edited. */
export interface StockTxn {
  id: number;
  stock_item: number;
  delta: number;
  reason: StockTxnReason;
  ref: string;
  actor: number | null;
  created_at: string;
}

/** `POST /api/v1/stock/{id}/txn` request body. `stock_item` comes from the
 * URL server-side and is never sent in the body (`apps.stock.api.
 * StockItemViewSet.txn`). `delta` is signed: positive for `receive`,
 * negative for `consume`; `adjust`/`correction` may be either sign. The
 * server rejects a delta that would drive `quantity_on_hand` negative with a
 * `400` (RFC-7807) — this is client-side pre-validation only. */
export interface StockTxnPayload {
  reason: StockTxnReason;
  delta: number;
  ref?: string;
}

/** `POST /api/v1/stock/{id}/txn` response body
 * (`apps.stock.api.StockItemViewSet.txn`) — the updated `StockItem` plus the
 * ledger row just created, and a server-computed `low_stock` flag so the UI
 * doesn't have to re-derive the threshold comparison itself. */
export interface StockTxnResponse {
  stock_item: StockItem;
  txn: StockTxn;
  low_stock: boolean;
}

/** `GET/POST/PATCH /api/v1/reorder-requests` (`apps.stock.serializers.
 * ReorderRequestSerializer`). `requested_by`/`approved_by` are user ids (not
 * nested) — the screen shows "you" for the caller's own requests and falls
 * back to the raw id otherwise (no user-directory endpoint exists yet). */
export interface ReorderRequest {
  id: number;
  stock_item: number;
  requested_by: number | null;
  approved_by: number | null;
  quantity: number;
  status: ReorderRequestStatus;
  note: string;
  requested_at: string;
  decided_at: string | null;
  created_at: string;
  updated_at: string;
}

/** `GET /api/v1/reorder-requests` query params
 * (`apps.stock.api.ReorderRequestViewSet.filterset_fields = ["status"]`). */
export interface ReorderRequestListParams extends ListParams {
  status?: ReorderRequestStatus;
  stock_item?: number;
}

/** `POST /api/v1/reorder-requests` request body. `stock_item` targets the
 * `StockItem` to reorder; `requested_by`/`status` are server-derived
 * (caller/`open`) and never client-writable on create. */
export interface ReorderRequestCreatePayload {
  stock_item: number;
  quantity: number;
  note?: string;
}

/** `PATCH /api/v1/reorder-requests/{id}` request body — drives status
 * transitions (`open -> approved -> ordered -> received`, `cancelled` from
 * any non-terminal state) or a plain field edit (`quantity`/`note`) on a
 * still-open request. Only one of `status` or the plain fields is typically
 * sent per call; the server validates the transition either way. */
export interface ReorderRequestUpdatePayload {
  status?: ReorderRequestStatus;
  quantity?: number;
  note?: string;
}

// --- Reservations & checkout (T3.4; `apps.reservations.serializers`/
// `apps.reservations.api`) — `docs/api-and-ui.md` "Reservations & checkout"
// table: `GET /reservations` (list/calendar feed, `?from&to`),
// `POST /reservations` (create), `POST /reservations/{id}/approve|reject|
// cancel`. Checkout endpoints (`POST /checkouts`, etc.) are T3.3/T3.5 — not
// added here (out of scope for this task, see T3.4 boundary note).

/** `Reservation.Status` choices (`backend/apps/reservations/models.py::
 * Reservation.Status`). `ACTIVE_STATUSES` (the ones that participate in the
 * F4 no-overlap exclusion constraint) are `pending`/`approved`/`fulfilled`.
 * `completed` is a terminal status set when a `fulfilled` reservation's
 * checkout is checked in — the asset was used and returned, and (unlike
 * `fulfilled`) its time window is free for others to rebook. */
export type ReservationStatus =
  | "pending"
  | "approved"
  | "rejected"
  | "cancelled"
  | "fulfilled"
  | "completed"
  | "expired";

/** `GET /api/v1/reservations` (list row / calendar feed item) / detail —
 * `apps.reservations.serializers.ReservationSerializer`. `asset`/`project`/
 * `approver` are plain ids (not nested) — the screen resolves asset names
 * separately, same "id, not nested" convention as `StockItem.asset`. `user`
 * (the requester) IS nested — `{id, email, name}`, same `AppUser` shape as
 * every other nested-user field in this API (see `AppUser` below) — so the
 * UI can render "Requested by: {name}" without a second lookup. */
export interface Reservation {
  id: number;
  asset: number;
  user: AppUser;
  project: number | null;
  start_at: string;
  end_at: string;
  status: ReservationStatus;
  approver: number | null;
  approval_note: string;
  created_at: string;
  updated_at: string;
}

/** `POST /api/v1/reservations` request body. `user`/`status`/`approver`/
 * `approval_note` are never client-writable (server-derived — see
 * `ReservationSerializer` doc comment: requester is always the caller,
 * status is auto-approve-vs-pending per `Category.requires_approval`). */
export interface ReservationCreatePayload {
  asset: number;
  start_at: string;
  end_at: string;
  project?: number | null;
}

/** `GET /api/v1/reservations` query params
 * (`apps.reservations.api.ReservationViewSet`: `filterset_fields =
 * ["status", "asset", "user"]`, `ordering_fields = ["start_at", "end_at",
 * "created_at"]`). `from`/`to` are the calendar-feed window (ISO-8601
 * datetimes) — restricts to reservations whose `[start_at, end_at)` window
 * overlaps `[from, to)`; either bound may be supplied alone. */
export interface ReservationListParams extends ListParams {
  from?: string;
  to?: string;
  status?: ReservationStatus;
  asset?: number;
  user?: number;
  ordering?: "start_at" | "-start_at" | "end_at" | "-end_at" | "created_at" | "-created_at";
}

// --- Checkouts (T3.5; `apps.reservations.checkout.CheckoutViewSet`/
// `CheckoutSerializer`) — `docs/api-and-ui.md` "Reservations & checkout"
// table: `POST /checkouts` (optionally from a reservation),
// `POST /checkouts/{id}/checkin`, `POST /checkouts/{id}/override-return`,
// `GET /checkouts?open=true&overdue=true`.

/** `GET/POST /api/v1/checkouts` (`apps.reservations.checkout.
 * CheckoutSerializer`). `asset`/`user`/`reservation` are plain ids (not
 * nested) — same "id, not nested" convention as `Reservation`/`StockItem`.
 * `is_open`/`is_overdue` are server-computed read-only flags — CLAUDE.md/task
 * note: never recompute the overdue logic client-side, always trust these. */
export interface Checkout {
  id: number;
  asset: number;
  user: number;
  reservation: number | null;
  checked_out_at: string;
  due_at: string;
  checked_in_at: string | null;
  checkout_condition: string;
  checkin_condition: string;
  is_open: boolean;
  is_overdue: boolean;
  created_at: string;
  updated_at: string;
}

/** `POST /api/v1/checkouts` request body. `user`/`checked_out_at`/
 * `checked_in_at`/`checkin_condition` are never client-writable (server-
 * derived — `apps.reservations.checkout.CheckoutSerializer.create`).
 * `reservation` is optional: link an approved/fulfilled reservation
 * belonging to the caller for the same asset, or omit for a direct walk-up
 * checkout — rejected with a `400` if the asset's category
 * `requires_approval` and no reservation is supplied (unless the caller also
 * holds `reservation.approve` in scope). */
export interface CheckoutCreatePayload {
  asset: number;
  due_at: string;
  reservation?: number;
  checkout_condition?: string;
}

/** Body for `POST /api/v1/checkouts/{id}/checkin` and
 * `POST /api/v1/checkouts/{id}/override-return` — the only client-writable
 * field is the returned condition note. Both are idempotent server-side
 * (`apps.reservations.checkout.perform_checkin`): calling either on an
 * already-checked-in checkout is a documented no-op, not an error. */
export interface CheckinPayload {
  checkin_condition?: string;
}

/** `GET /api/v1/checkouts` query params (`apps.reservations.checkout.
 * CheckoutViewSet.get_queryset`). `asset`/`reservation` are manual
 * query-param filters (same "let a bogus id resolve to no rows" tolerance as
 * `open`/`overdue`, not `django_filter` `filterset_fields`) — the documented
 * way to find "the (open) checkout for this specific asset/reservation"
 * (e.g. `?reservation=<id>&open=true`) without a client-side scan. **No
 * `user` query filter is wired server-side**; `search` is inherited from
 * `ListParams` but similarly unimplemented — do not send either until a
 * backend task adds them (flagged, same as `StockListParams`'s missing
 * `search`). Listing is already scoped server-side to the caller's own
 * checkouts UNION their `checkout.manage`/`checkout.override` project scope
 * — there is no `?user=me` param to pass (nor is one needed). */
export interface CheckoutListParams extends ListParams {
  open?: boolean;
  overdue?: boolean;
  asset?: number;
  reservation?: number;
  ordering?:
    | "due_at"
    | "-due_at"
    | "checked_out_at"
    | "-checked_out_at"
    | "checked_in_at"
    | "-checked_in_at"
    | "created_at"
    | "-created_at";
}

// --- Dashboard (T5.5/T5.6; `apps.dashboard.api`/`apps.dashboard.serializers`)
// -- `docs/api-and-ui.md` "Maintenance, labels, import/export, dashboard":
// `GET /dashboard/summary` (aggregates, cached server-side). Plain `path()`
// route (no trailing slash, not router-registered — see
// `apps.dashboard.api` module docstring), unlike every other endpoint below.

/** One row of `DashboardSummary.totals_by_category`
 * (`apps.dashboard.serializers.CategoryTotalSerializer`). `category_id`/
 * `category_name` are `null` for assets with no category set. */
export interface CategoryTotal {
  category_id: number | null;
  category_name: string | null;
  count: number;
}

/** One row of `DashboardSummary.per_project_allocation`
 * (`apps.dashboard.serializers.ProjectAllocationSerializer`).
 * `project_id === null` represents the general (unassigned) pool. */
export interface ProjectAllocation {
  project_id: number | null;
  project_name: string;
  count: number;
}

/** `GET /api/v1/dashboard/summary` response
 * (`apps.dashboard.serializers.DashboardSummarySerializer`). Every count is
 * already scoped server-side to the caller's viewable projects (tenant-wide
 * for Admin, own project(s) only for a pure ProjectLead) — render as-is,
 * never re-filter client-side. Cached server-side (30s TTL) — no client
 * caching needed, just fetch on screen mount. */
export interface DashboardSummary {
  totals_by_category: CategoryTotal[];
  currently_out: number;
  overdue: number;
  low_stock: number;
  upcoming_reservations: number;
  upcoming_reservations_window_days: number;
  per_project_allocation: ProjectAllocation[];
  generated_at: string;
}

// --- Notification preferences (T5.1/T5.6; `apps.notifications.api`/
// `apps.notifications.serializers`) — `docs/api-and-ui.md`:
// `GET/PATCH /api/v1/notification-prefs` ("Per-user prefs").

/** The `event_type` keys actually emitted server-side
 * (`apps.notifications.receivers.EVENT_*` constants) — used to seed the "My
 * Notifications" screen's fixed row list even before the user has an
 * explicit `NotificationPref` row for every one of them (a user with no
 * explicit preference yet defaults to enabled — see
 * `apps.notifications.api` module docstring). */
export const NOTIFICATION_EVENT_TYPES = [
  "reservation_confirmed",
  "approval_request",
  "approval_decision",
  "overdue_reminder",
  "low_stock_alert",
] as const;

export type NotificationEventType = (typeof NOTIFICATION_EVENT_TYPES)[number];

/** `GET/PATCH /api/v1/notification-prefs/{event_type}` row shape
 * (`apps.notifications.serializers.NotificationPrefSerializer`). Always the
 * CALLER's own row (`apps.notifications.api.NotificationPrefViewSet.
 * get_queryset`/`get_object` never resolve another user's) — there is no
 * `user` field on the wire because of that. */
export interface NotificationPref {
  id: number;
  event_type: string;
  email_enabled: boolean;
  created_at: string;
  updated_at: string;
}

/** `PATCH /api/v1/notification-prefs/{event_type}` request body — the only
 * client-writable field. */
export interface NotificationPrefUpdatePayload {
  email_enabled: boolean;
}

// --- Email settings (`GET/PUT/PATCH /api/v1/notifications/email-settings`;
// `apps.notifications.serializers.EmailSettingsSerializer`) — tenant admins
// configure Brevo/console email delivery from the UI. Singleton per tenant
// (no list, no id in the URL); gated server-side on `tenant.manage` for BOTH
// read and write.

/** Read shape. The raw/encrypted API key is NEVER included — only
 * `has_api_key`/`api_key_last4` (a harmless last-4-chars hint), same UX
 * pattern as most "add a payment card" UIs. */
export interface EmailSettings {
  provider: "console" | "brevo";
  sender_email: string;
  reply_to: string;
  api_key_last4: string;
  has_api_key: boolean;
  updated_at: string;
}

/** `PUT/PATCH /api/v1/notifications/email-settings` request body.
 * `api_key` is write-only and has "omit vs blank" semantics (see
 * `EmailSettingsSerializer` docstring): omit the field entirely to leave the
 * stored key untouched, send `""` to explicitly clear it, or a non-empty
 * string to set/replace it — never always send it on every save. */
export interface EmailSettingsUpdate {
  provider: "console" | "brevo";
  sender_email: string;
  reply_to: string;
  api_key?: string;
}

/** `POST /api/v1/notifications/email-settings/test` response — always sends
 * to the logged-in caller's own email using the tenant's already-saved
 * settings (no request body). `provider` is the resolved provider class
 * name (e.g. `"BrevoProvider"`/`"ConsoleProvider"`), not the
 * `"console"|"brevo"` slug. */
export interface EmailSettingsTestResult {
  status: "sent";
  provider: string;
  sent_to: string;
}

// --- Session settings (`GET/PATCH /api/v1/tenancy/session-settings`;
// `apps.tenancy.serializers.SessionSettingsSerializer`) — tenant admins
// configure the idle/absolute session timeout enforced by
// `SessionTimeoutMiddleware`. Singleton per tenant (no list, no id in the
// URL); gated server-side on `tenant.manage` for BOTH read and write, same
// pattern as `EmailSettings` above.

/** Read shape. Bounds enforced server-side: `idle_timeout_minutes` 5–480,
 * `absolute_timeout_hours` 1–720. */
export interface SessionSettings {
  idle_timeout_minutes: number;
  absolute_timeout_hours: number;
  updated_at: string;
}

/** `PATCH /api/v1/tenancy/session-settings` request body — both fields
 * optional (partial update), but out-of-bounds values 400 with RFC-7807
 * field errors (`problem.errors.idle_timeout_minutes` etc). */
export interface SessionSettingsUpdate {
  idle_timeout_minutes?: number;
  absolute_timeout_hours?: number;
}

// --- Audit log (T5.3/T5.6; `apps.audit.api`/`apps.audit.serializers`) —
// `docs/api-and-ui.md`: `GET /api/v1/audit` ("Audit log (scoped)"),
// "Audit Log" screen: "Filterable immutable history".

/** `GET /api/v1/audit` (list row / detail) —
 * `apps.audit.serializers.AuditLogSerializer`. Read-only (append-only log,
 * no write endpoint at all — see `apps.audit.api` module docstring).
 * `before`/`after` are opaque JSON snapshots (shape varies per
 * `entity_type`/`action`) — rendered as raw JSON in the UI, not parsed
 * per-entity-type. `actor`/`actor_email`/`actor_name` are all `null` for a
 * system-initiated entry with no acting user. */
export interface AuditLogEntry {
  id: number;
  actor: number | null;
  actor_email: string | null;
  actor_name: string | null;
  action: string;
  entity_type: string;
  entity_id: string;
  before: unknown;
  after: unknown;
  ip: string | null;
  created_at: string;
}

/** `GET /api/v1/audit` query params (`apps.audit.api.AuditLogFilterSet`/
 * `AuditLogViewSet`). All plain equality/range filters — `actor` is a bare
 * user-id match, not a search. `ordering` is restricted to `created_at`
 * server-side (`ordering_fields = ["created_at"]`). */
export interface AuditLogListParams extends ListParams {
  entity_type?: string;
  entity_id?: string;
  action?: string;
  actor?: number;
  created_after?: string;
  created_before?: string;
  ordering?: "created_at" | "-created_at";
}

/** `AssetCursorPagination`'s envelope shape (`rest_framework.pagination.
 * CursorPagination`) — `next`/`previous` are opaque full URLs (no `count`,
 * unlike `Paginated<T>`'s page-number envelope) since cursor pagination
 * can't cheaply compute a total. Only relevant if a caller opts into
 * `?cursor=`; the List screen itself uses the default page-number mode. */
export interface CursorPaginated<T> {
  next: string | null;
  previous: string | null;
  results: T[];
}

// --- Jobs / label PDF generation (T4.5; `apps.jobs.api`/`apps.labels.api`) ---

/** `Job.Status` (`backend/apps/jobs/models.py`) — polled via `GET /api/v1/
 * jobs/{id}` until it lands on `succeeded`/`failed`. */
export type JobStatus = "queued" | "running" | "succeeded" | "failed";

/** `GET /api/v1/jobs/{id}` response / the body `POST /api/v1/labels/generate`
 * returns immediately (`202`, status `queued`). `download_url` is only
 * non-null once `status === "succeeded"` — a plain `/media/...` path served
 * directly by nginx (same trust model `Attachment.storage_key` already uses,
 * see `apps.jobs.serializers.JobSerializer` doc comment), not a JSON/API
 * response — fetch/`<a href>` it directly. */
export interface Job {
  id: string;
  job_type: string;
  status: JobStatus;
  error: string;
  result_filename: string;
  download_url: string | null;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  finished_at: string | null;
}

/** `?template=` for `POST /api/v1/labels/generate` (`apps.labels.templates.
 * SHEET_TEMPLATES` — the two Avery defaults documented for MVP, `docs/tasks/
 * M4-mobile-scan-labels.md`'s Q9 default). */
export type LabelSheetTemplate = "avery_5160" | "avery_5163" | "single";

/** `POST /api/v1/labels/generate` request body (T4.5). Requires
 * `label.generate` (scoped — Admin tenant-wide, ProjectLead within their own
 * project's assets); any requested id outside the caller's tenant/scope is
 * silently dropped server-side rather than erroring, UNLESS that leaves zero
 * assets (400). */
export interface LabelGenerateRequest {
  asset_ids: number[];
  template: LabelSheetTemplate;
}

// --- Bulk import (T6.2; `apps.imports.api`/`apps.imports.serializers`) ---
// `POST /api/v1/imports` (multipart upload, dry-run), `GET /api/v1/imports/
// {id}` (poll), `POST /api/v1/imports/{id}/commit`. Column-mapping target
// vocabulary is fixed server-side (`apps.imports.services.ALL_TARGETS`):
// the 7 "core" asset fields, plus `"custom"` (per-row/per-category custom
// field lookup by header name) and `"ignore"` (drop the column).

export type ImportJobStatus =
  | "pending"
  | "dry_run_running"
  | "dry_run_succeeded"
  | "dry_run_failed"
  | "committing"
  | "committed"
  | "commit_failed";

/** A column-mapping override: `{"<spreadsheet header>": "<target>"}`. Any
 * header omitted falls back to the server's auto-detected default. */
export type ImportMapping = Record<string, string>;

export const IMPORT_CORE_TARGETS = [
  "name",
  "category",
  "location",
  "project",
  "status",
  "condition",
  "tags",
] as const;
export type ImportCoreTarget = (typeof IMPORT_CORE_TARGETS)[number];
/** Every valid mapping target — the 7 core fields above, plus `"custom"`
 * (per-category custom-field lookup by header name/key) and `"ignore"`. */
export type ImportTarget = ImportCoreTarget | "custom" | "ignore";

/** One resolved row of a dry-run/commit report
 * (`apps.imports.services.ResolvedRow.to_report_dict`). `row_number` is
 * 1-based and counts the header row as row 1 (so `2` = the first data row —
 * matches what a user sees if they open the spreadsheet themselves).
 * `errors` is non-empty exactly when the row is invalid; a `custom_field_
 * values` key nests `{field_key: message}` from `validate_custom_field_
 * values`, every other key is a plain string message. */
export interface ImportReportRow {
  row_number: number;
  values: {
    name: string | null;
    category: string | null;
    location: string | null;
    project: string | null;
    tags: string[];
    status: string;
    condition: string;
    custom_field_values: Record<string, unknown>;
  };
  errors: Record<string, unknown>;
}

/** `ImportJob.report` (`apps.imports.services.build_report`/
 * `commit_import_rows`) — the latest dry-run OR commit-pass validation
 * result. `null` until the first dry-run task has actually run. */
export interface ImportReport {
  resolved_mapping: ImportMapping;
  rows: ImportReportRow[];
  total_rows: number;
  valid_count: number;
  invalid_count: number;
}

/** Minimal nested view of the underlying `Job` (`apps.imports.serializers.
 * ImportJobJobSerializer`) — just enough to know status/error without a
 * second `GET /api/v1/jobs/{id}` round trip; this screen polls `GET
 * /api/v1/imports/{id}` directly instead, which is richer. */
export interface ImportJobJob {
  id: string;
  status: JobStatus;
  error: string;
}

/** `GET /api/v1/imports/{id}` / the body `POST /api/v1/imports` and `POST
 * /api/v1/imports/{id}/commit` return immediately (T6.2). */
export interface ImportJob {
  id: number;
  status: ImportJobStatus;
  source_filename: string;
  mapping: ImportMapping;
  report: ImportReport | null;
  created_asset_ids: number[];
  dry_run_job: ImportJobJob | null;
  commit_job: ImportJobJob | null;
  created_at: string;
  updated_at: string;
}

// --- Users & Roles admin (Users & Roles screen; apps.accounts.api.UserViewSet,
// apps.rbac.api.MembershipViewSet/RoleViewSet) ---

/** `GET /api/v1/users/` row shape / the user fields of `POST /api/v1/users`'s
 * response (`apps.accounts.serializers.UserSerializer`) — id/email/name only,
 * deliberately excludes anything password-related. */
export interface AppUser {
  id: number;
  email: string;
  name: string;
}

/** `POST /api/v1/users/` request body (`apps.accounts.serializers.
 * CreateUserSerializer`). No `password` field — always server-generated. */
export interface CreateUserPayload {
  email: string;
  name?: string;
}

/** `POST /api/v1/users/` response (`apps.accounts.api.UserViewSet.create`):
 * the plain `AppUser` shape plus a ONE-TIME `password` field (the server's
 * generated initial password) — never returned again by any other endpoint.
 * Handle with care: never log it, never put it in a URL, never stash it
 * anywhere longer-lived than the one-time-reveal modal needs. */
export interface CreatedUser extends AppUser {
  password: string;
}

/** `GET /api/v1/roles/` row shape (`apps.rbac.serializers.RoleSerializer`) —
 * the tenant's 4 seeded system roles. */
export interface Role {
  id: number;
  key: string;
  name: string;
  /** True for the 4 seeded system roles — they can be edited but never
   * deleted or re-keyed (`apps.rbac.api.RoleViewSet`). */
  is_system: boolean;
  /** True once an admin has edited this role away from the shipped
   * `docs/rbac.md` §3 defaults. Drives the "Reset to defaults" affordance. */
  is_customized: boolean;
  /** The role's full grant set. WRITABLE on PATCH — sending it REPLACES the
   * set (an unchecked box means "revoked", not "unchanged"). */
  permission_keys: string[];
  /** How many memberships currently hold this role (a role in use cannot be
   * deleted). */
  member_count: number;
}

/** `GET /api/v1/permissions` — the fixed, system-wide permission vocabulary
 * (`apps.rbac.permission_keys.PERMISSION_LABELS`). `group` is the key's
 * dotted prefix, for UI sectioning only. */
export interface PermissionCatalogEntry {
  id: number;
  key: string;
  label: string;
  group: string;
}

export type PermissionOverrideEffect = "grant" | "deny";

/** `GET/PUT /api/v1/users/{id}/permissions`
 * (`apps.rbac.api.UserPermissionsView`). `overrides` is the per-user
 * exception layer; `effective_permission_keys` is what the server will
 * actually enforce tenant-wide, i.e. roles + grants − denies. */
export interface UserPermissions {
  user: number;
  user_email: string;
  role_permission_keys: string[];
  overrides: Record<string, PermissionOverrideEffect>;
  effective_permission_keys: string[];
}

export interface RoleWritePayload {
  key?: string;
  name?: string;
  permission_keys?: string[];
}

/** `GET /api/v1/memberships/` row shape / `POST`+`PATCH` response
 * (`apps.rbac.serializers.MembershipSerializer`). `project`/`project_name`
 * are `null` for a tenant-wide grant. */
export interface Membership {
  id: number;
  user: number;
  user_email: string;
  role: number;
  role_key: string;
  role_name: string;
  project: number | null;
  project_name: string | null;
  created_at: string;
}

/** `POST /api/v1/memberships/` request body. `project` omitted/`null` =
 * tenant-wide. */
export interface MembershipCreatePayload {
  user: number;
  role: number;
  project?: number | null;
}

/** `PATCH /api/v1/memberships/{id}/` request body — `role` is the ONLY
 * editable field once a Membership exists (`user`/`project` are read-only
 * server-side past creation, `apps.rbac.serializers.MembershipSerializer.
 * get_fields`). */
export interface MembershipUpdatePayload {
  role: number;
}

/** `GET /api/v1/memberships/` query params (`apps.rbac.api.
 * MembershipFilterSet`) — plain id-equality filters. */
export interface MembershipListParams extends ListParams {
  user?: number;
  role?: number;
  project?: number;
}


/** `GET /api/v1/assets/{id}/expense-prefill` — the asset's own purchase facts
 * relabeled into expense vocabulary, so the project expense form can offer
 * "fetch from asset" instead of re-keying. `documents` are the asset's
 * `doc`-kind attachments, offered as copy candidates; copying one is a
 * separate call (`copyAssetAttachmentToExpense`). */
export interface AssetExpensePrefill {
  asset: number;
  asset_name: string;
  project: number | null;
  /** Decimal as a string (never a float), matching `Expense.amount`. */
  amount: string | null;
  currency: string;
  date: string | null;
  vendor: string;
  description: string;
  url: string;
  /** Every attachment on the asset, ranked by the server: financial types
   * first (invoice -> receipt -> purchase order -> quote), then the rest,
   * newest first within each band. `documents[0]` is the right default
   * selection — the client does not re-derive this ordering. */
  documents: {
    id: number;
    filename: string;
    content_type: string;
    size: number;
    kind: string;
    doc_type: AttachmentDocType;
    is_financial: boolean;
    created_at: string;
  }[];
}
