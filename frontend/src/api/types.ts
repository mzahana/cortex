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
  | "json";

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

/** `GET/POST/PATCH /api/v1/projects` (`apps.catalog.serializers.ProjectSerializer`,
 * registered as `ProjectViewSet` in `apps.catalog.api`). */
export interface Project {
  id: number;
  name: string;
  lead_user: number | null;
  is_active: boolean;
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
export interface Attachment {
  id: number;
  kind: "photo" | "doc";
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
 * F4 no-overlap exclusion constraint) are `pending`/`approved`/`fulfilled`. */
export type ReservationStatus =
  | "pending"
  | "approved"
  | "rejected"
  | "cancelled"
  | "fulfilled"
  | "expired";

/** `GET /api/v1/reservations` (list row / calendar feed item) / detail —
 * `apps.reservations.serializers.ReservationSerializer`. `asset`/`user`/
 * `project`/`approver` are plain ids (not nested) — the screen resolves
 * asset/user names separately, same "id, not nested" convention as
 * `StockItem.asset`. */
export interface Reservation {
  id: number;
  asset: number;
  user: number;
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
