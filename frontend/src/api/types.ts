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
 * never send it in the body. NOTE: the backend contract only exposes
 * `GET`/`POST` on this sub-resource — there is no documented endpoint to
 * edit/reorder/delete an existing field def once created (flagged for
 * backend-engineer; see `CategoryFieldsPanel` for how the UI handles this
 * gap). */
export interface CustomFieldDefCreatePayload {
  key: string;
  label: string;
  data_type: CustomFieldDataType;
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
