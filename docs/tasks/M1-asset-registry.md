# M1 — Asset registry core (MVP)

**Goal:** the heterogeneous asset registry with custom fields, trees, search, and the
list/detail/edit UI. This is the backbone the rest of the MVP hangs on.
**Dep:** M0. **Effort:** L.
**Milestone exit:** F2 acceptance met; 10k-row seed searches < 500 ms server-side.
Refs: `docs/data-model.md`, `docs/features.md` F2/F10, `docs/api-and-ui.md`.

---

### T1.1 · → backend-engineer · deps: M0
**Do:** Category (self-referential tree; `default_is_consumable`, `requires_approval`,
`requires_calibration`), CustomFieldDef (typed: text|int|float|bool|date|enum|json,
unit, enum_options, required, order), Location (self-referential tree), Project, Tag,
TagLink models + tenant-scoped serializers/viewsets and their endpoints from
`docs/api-and-ui.md` (categories + `/{id}/fields`, locations, projects, tags).
**Files:** `backend/apps/catalog/`.
**Exit:** Admin can build a Category tree with custom fields and a Location tree via API;
all reads tenant-scoped; `category.manage`/`location.manage` enforced.

### T1.2 · → backend-engineer · deps: T1.1
**Do:** Asset model per `docs/data-model.md` (identity incl. uuid public code +
`qr_token`, category, name/description, `is_consumable`, `project_id`, physical/
commercial fields, `status` lifecycle, `condition`, `current_workload_user_id`,
`search_vector`). AssetFieldValue (JSONB). Attachment (key in DB, binary on volume via
django-storages). Asset CRUD + retire endpoint + attachment upload endpoint. Enforce
the RBAC matrix (`asset.create/edit/retire/attach`, scoped).
**Files:** `backend/apps/assets/`.
**Exit:** create a Compute asset with GPU/VRAM custom-field values and a Component
consumable; retire hides from default lists but retains the row; attachment stored on
the volume, only the key in DB.

### T1.3 · → db-migration-specialist · deps: T1.2
**Do:** Migrations for T1.1/T1.2. All indexes from `docs/data-model.md` §3: composite
`(tenant_id, status|category_id|location_id|project_id)`; GIN on `search_vector`;
`pg_trgm` GIN on `name` and `serial_number`; JSONB GIN on `asset_field_value.value`.
`search_vector` maintained by a **trigger** over name/serial/tags/custom values. RLS
policies on every new tenant-owned table.
**Files:** `backend/apps/assets/migrations/`, `backend/apps/catalog/migrations/`.
**Exit:** migrate up/down clean; a FTS query uses the GIN index (verify with EXPLAIN);
RLS on new tables verified.

### T1.4 · → backend-engineer · deps: T1.3
**Do:** Asset list endpoint: server-side pagination (`?page`,`?page_size`, cursor
option), `?search=` (FTS + `pg_trgm` fuzzy over name/serial/custom values), `?ordering=`,
and filters `category,status,location,project,tag,is_consumable`. Kill N+1s with
`select_related`/`prefetch_related`.
**Files:** `backend/apps/assets/api.py`.
**Exit:** filterable by every documented facet; bounded, asserted query count.

### T1.5 · → frontend-engineer · deps: T1.1, T0.7
**Do:** Admin: Categories & Fields screen (tree editor + custom-field defs + approval
flags) and Admin: Locations screen (tree). Reusable tree component.
**Files:** `frontend/src/screens/admin/*`.
**Exit:** an admin can build the category/location trees and field defs end-to-end.

### T1.6 · → frontend-engineer · deps: T1.4
**Do:** Asset List (server-side search + filters + tags; virtualized; card + table
views) and Asset Detail (specs from custom fields, photos, status, location, history
placeholder; action buttons gated by `/me` permissions). Actions that belong to later
milestones (reserve/checkout/label/issue) are present-but-disabled stubs.
**Files:** `frontend/src/screens/assets/*`.
**Exit:** list is paginated/filterable against real data; detail renders custom fields.

### T1.7 · → frontend-engineer · deps: T1.2
**Do:** Asset Create/Edit — a **category-driven dynamic form** rendering fields from the
selected category's CustomFieldDef list (respecting type/required/unit/enum_options/
order), plus location picker and photo attach.
**Files:** `frontend/src/screens/assets/AssetForm.tsx`.
**Exit:** selecting a category changes the field set; a valid submit creates the asset
with custom values.

### T1.8 · → qa-test-engineer · deps: T1.4 · **perf gate**
**Do:** A reusable **10k+ asset seed** (management command / factories, realistic
categories + custom fields + tags). Perf tests asserting paginated list and FTS search
return < 500 ms server-side with bounded query counts (F10). Filter-correctness tests.
**Exit:** perf target met and asserted in CI (or documented if hardware-bound); N+1s
absent.

### T1.9 · → qa-test-engineer + code-reviewer · deps: T1.2–T1.7 · **milestone gate**
**Do:** F2 acceptance tests (create both asset types with custom fields; list filterable;
retired hidden-but-retained). code-reviewer gates the M1 diff with focus on tenant
scoping of every new query.
**Exit:** F2 met; review signed off.

> **Open question (Q7):** confirm the must-have custom fields per category (esp.
> Compute/GPU, Drone electronics) to seed correct CustomFieldDefs. Until answered, seed
> a documented reasonable default and flag it.
