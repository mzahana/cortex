import { useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Center,
  FileButton,
  Group,
  Image,
  Loader,
  Modal,
  NumberInput,
  Select,
  SimpleGrid,
  Stack,
  Switch,
  TagsInput,
  Text,
  Textarea,
  TextInput,
  Title,
} from "@mantine/core";
import { useForm } from "@mantine/form";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api, ApiError } from "../../api/client";
import {
  ASSET_ATTACH,
  ASSET_CREATE,
  ASSET_EDIT,
  CATEGORY_MANAGE,
  hasAnyAssetPermission,
  hasAssetPermission,
  hasPermission,
  LOCATION_MANAGE,
  STOCK_ADJUST,
} from "../../api/permissions";
import { useAuth } from "../../hooks/useAuth";
import { AppLayout } from "../../layout/AppLayout";
import type {
  Asset,
  AssetStatus,
  AssetWritePayload,
  Category,
  CustomFieldDef,
  Location,
  Project,
  StockItemCreatePayload,
} from "../../api/types";
import { buildTree, flattenForSelect } from "../../components/treeUtils";
import { STATUS_OPTIONS } from "./assetConstants";
import { CustomFieldInputs } from "./CustomFieldInputs";
import { CategoryFormModal } from "../admin/CategoryFormModal";
import { LocationFormModal } from "../admin/LocationFormModal";

interface FormValues {
  category: string | null;
  name: string;
  description: string;
  is_consumable: boolean;
  project: string | null;
  serial_number: string;
  manufacturer: string;
  model: string;
  location: string | null;
  purchase_date: string;
  purchase_cost: number | "";
  currency: string;
  warranty_expiry: string;
  supplier: string;
  url: string;
  status: AssetStatus;
  condition: string;
  tags: string[];
}

const EMPTY_VALUES: FormValues = {
  category: null,
  name: "",
  description: "",
  is_consumable: false,
  project: null,
  serial_number: "",
  manufacturer: "",
  model: "",
  location: null,
  purchase_date: "",
  purchase_cost: "",
  currency: "",
  warranty_expiry: "",
  supplier: "",
  url: "",
  status: "available",
  condition: "",
  tags: [],
};

// `status` is edit-able for ordinary lifecycle moves only — the `retired`
// transition is server-blocked outside `POST /assets/{id}/retire`
// (`AssetSerializer.validate`), so it's never offered here even though it's
// a valid `Asset.Status` member elsewhere (`assetConstants.STATUS_OPTIONS`).
const STATUS_SELECT_OPTIONS = STATUS_OPTIONS.filter((o) => o.value !== "retired");

function computeJsonDrafts(defs: CustomFieldDef[], values: Record<string, unknown>): Record<string, string> {
  const drafts: Record<string, string> = {};
  for (const def of defs) {
    if (def.data_type === "json") {
      const present = Object.prototype.hasOwnProperty.call(values, def.key);
      drafts[def.key] = JSON.stringify(present ? values[def.key] : null, null, 2);
    }
  }
  return drafts;
}

function hasEnteredValue(value: unknown): boolean {
  if (value === undefined || value === null || value === "") return false;
  if (Array.isArray(value) && value.length === 0) return false;
  return true;
}

/** Client-side mirror of the server's `URLValidator` (absolute URL, http/https
 * scheme only) for `data_type: "url"` custom fields — catches the common typo
 * cases (missing scheme, stray spaces) before a round trip to the server. */
function isValidAbsoluteUrl(value: string): boolean {
  try {
    const parsed = new URL(value.trim());
    return parsed.protocol === "http:" || parsed.protocol === "https:";
  } catch {
    return false;
  }
}

function messageFromDetail(detail: unknown): string {
  if (Array.isArray(detail)) return detail.join(" ");
  if (typeof detail === "object" && detail !== null) return JSON.stringify(detail);
  return String(detail);
}

/**
 * Asset Create/Edit (T1.7, docs/api-and-ui.md "Asset Create/Edit":
 * "Category-driven dynamic form (custom fields), photo capture, location
 * picker"). One screen serves both routes:
 * - `/assets/new` (create, `POST /api/v1/assets`)
 * - `/assets/:id/edit` (edit, `PATCH /api/v1/assets/{id}`, pre-populated)
 *
 * The custom-field section is entirely driven by the selected category's
 * live `CustomFieldDef[]` (`GET /categories/{id}/fields`) — see
 * `CustomFieldInputs` for the `data_type` -> input mapping. Changing the
 * category re-fetches that list and remaps entered custom values: values for
 * keys still present in the new field set are kept, values for keys that
 * would be silently dropped are confirmed with the user first
 * (`pendingSwitch` below).
 *
 * Feature C (post-MVP gap fill): when "Consumable" is on, a "Consumable
 * stock" card collects unit/initial-quantity/reorder config and calls
 * `POST /stock/` (+ a `receive` txn for a nonzero initial quantity, Contract
 * 3) as a follow-up to the asset save — on create, right after
 * `createAsset` succeeds; on edit, via its own standalone "Set up stock
 * tracking" button (the asset already exists, so there's no single combined
 * submit). ASSUMPTION/flagged gap: `GET /stock` has no `?asset=` filter
 * (`apps.stock.api.StockItemViewSet.get_queryset` — confirmed by reading the
 * backend directly), so this form can't proactively know on load whether an
 * existing consumable asset already has a `StockItem` — the block always
 * renders for a consumable asset and instead surfaces the server's own
 * "already has a StockItem" `400` (`errors.asset`) inline as a normal,
 * handled outcome, same CLAUDE.md pattern as every other write here.
 *
 * Client-side validation (required/type/enum) mirrors the server's own
 * `apps.assets.services.validate_custom_field_values` for immediate UX
 * feedback, but the SERVER is authoritative: a 400's `errors` (including
 * per-custom-field messages nested under `errors.custom_field_values`) are
 * surfaced back onto the exact inputs that caused them (`applyServerErrors`).
 */
export function AssetFormScreen() {
  const { id } = useParams<{ id: string }>();
  const isEdit = Boolean(id);
  const navigate = useNavigate();
  const { me } = useAuth();
  // M7 (`docs/tasks/M7-project-grants.md`): the project hub's Assets tab
  // links "New asset" to `/assets/new?project=<id>` so a new asset created
  // from within a project's context starts pre-scoped to it — read-only
  // preset (create only; an edit never has this query param since the
  // existing asset's own `project` always wins, see `existingAsset` load
  // below).
  const [searchParams] = useSearchParams();
  const presetProjectId = !isEdit ? searchParams.get("project") : null;

  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [categories, setCategories] = useState<Category[]>([]);
  const [locations, setLocations] = useState<Location[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [existingAsset, setExistingAsset] = useState<Asset | null>(null);

  const [fieldDefs, setFieldDefs] = useState<CustomFieldDef[]>([]);
  const [fieldDefsLoading, setFieldDefsLoading] = useState(false);
  const [fieldDefsError, setFieldDefsError] = useState<string | null>(null);

  const [customValues, setCustomValues] = useState<Record<string, unknown>>({});
  const [jsonDrafts, setJsonDrafts] = useState<Record<string, string>>({});
  const [jsonErrors, setJsonErrors] = useState<Record<string, string>>({});
  const [customFieldErrors, setCustomFieldErrors] = useState<Record<string, string>>({});

  const customValuesRef = useRef(customValues);
  useEffect(() => {
    customValuesRef.current = customValues;
  }, [customValues]);

  // Never auto-default `is_consumable` from a category pick once the user
  // has touched the switch themselves, or when editing an existing asset
  // (its current flag must never be silently overwritten by a category
  // change — docs/data-model.md §4, mirrors the server's create-only default
  // in `AssetSerializer.validate`).
  const isConsumableTouchedRef = useRef(isEdit);

  const [pendingSwitch, setPendingSwitch] = useState<{
    categoryId: number;
    fieldDefs: CustomFieldDef[];
    droppedLabels: string[];
  } | null>(null);

  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  // --- Feature C (post-MVP gap fill): set initial quantity/reorder config
  // for a consumable asset, `StockItemCreatePayload`/`Contract 3`. Local
  // state, not part of `form`/`AssetWritePayload` — `StockItem` is a
  // separate resource (`POST /stock/`), created as a follow-up call, never
  // sent as part of `createAsset`/`updateAsset`'s own body.
  const [stockUnit, setStockUnit] = useState("");
  const [stockInitialQty, setStockInitialQty] = useState<number | "">("");
  const [stockReorderThreshold, setStockReorderThreshold] = useState<number | "">(0);
  const [stockReorderTarget, setStockReorderTarget] = useState<number | "">(0);
  const [stockSetupBusy, setStockSetupBusy] = useState(false);
  const [stockSetupError, setStockSetupError] = useState<string | null>(null);
  const [stockSetupSuccess, setStockSetupSuccess] = useState<string | null>(null);
  // Set once a `StockItem` is confirmed to exist for this asset — either
  // this session's own setup succeeded, or the server told us one already
  // exists (`errors.asset`, `StockItemSerializer.validate_asset`). There is
  // no `GET /stock?asset=<id>` filter to check this proactively on load
  // (flagged for backend-engineer below) — see this file's module doc
  // comment for the full assumption.
  const [stockAlreadyTracked, setStockAlreadyTracked] = useState(false);
  // Set only when THIS handler invocation created the `StockItem` and the
  // follow-up "receive" txn (initial quantity) then failed — distinct from
  // `stockAlreadyTracked` alone, which is also true for a pre-existing item
  // discovered via the server's "already has a StockItem" 400. That
  // distinction matters: a pre-existing item has nothing to retry here (its
  // quantity, if any, was already set by whoever created it); a StockItem
  // this action just created is stuck at quantity 0 until the receive txn
  // succeeds, so it's the one case where re-attempting just that call (not
  // the whole create-flow, which would now 400) is the right recovery.
  const [pendingStockReceiveRetry, setPendingStockReceiveRetry] = useState<{ stockItemId: number } | null>(
    null,
  );

  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const resetFileRef = useRef<() => void>(null);

  // Inline "+ New" category/location creation (so the user doesn't have to
  // leave the Asset form to add a missing one) — reuses the existing admin
  // modals (`CategoryFormModal`/`LocationFormModal`) rather than duplicating
  // their create logic.
  const [categoryModalOpen, setCategoryModalOpen] = useState(false);
  const [locationModalOpen, setLocationModalOpen] = useState(false);

  const form = useForm<FormValues>({
    initialValues: presetProjectId ? { ...EMPTY_VALUES, project: presetProjectId } : EMPTY_VALUES,
    validate: {
      name: (v) => (v.trim() ? null : "Name is required."),
      category: (v) => (v ? null : "Category is required."),
      currency: (v) => (v && v.length > 3 ? "Use a 3-letter currency code (e.g. USD)." : null),
      // Mirrors `AssetSerializer.validate_url` for immediate feedback; the
      // server is still the authority (it stores only http/https, since the
      // detail screen renders this as a real link).
      url: (v) =>
        !v.trim() || /^https?:\/\//i.test(v.trim()) ? null : "Must start with http:// or https://.",
    },
  });

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setLoadError(null);
      try {
        const [cats, locs, projs] = await Promise.all([
          api.listAllCategories({ ordering: "name" }),
          api.listAllLocations({ ordering: "name" }),
          api.listAllProjects({ ordering: "name" }),
        ]);
        if (cancelled) return;
        setCategories(cats);
        setLocations(locs);
        setProjects(projs);

        if (isEdit && id) {
          const asset = await api.getAsset(Number(id));
          if (cancelled) return;
          setExistingAsset(asset);
          form.setValues({
            category: String(asset.category),
            name: asset.name,
            description: asset.description,
            is_consumable: asset.is_consumable,
            project: asset.project ? String(asset.project) : null,
            serial_number: asset.serial_number,
            manufacturer: asset.manufacturer,
            model: asset.model,
            location: asset.location ? String(asset.location) : null,
            purchase_date: asset.purchase_date ?? "",
            purchase_cost: asset.purchase_cost ? Number(asset.purchase_cost) : "",
            currency: asset.currency,
            warranty_expiry: asset.warranty_expiry ?? "",
            supplier: asset.supplier,
            url: asset.url,
            status: asset.status,
            condition: asset.condition,
            tags: asset.tags,
          });

          const defs = await api.listCategoryFields(asset.category);
          if (cancelled) return;
          setFieldDefs(defs);
          setCustomValues({ ...asset.field_values });
          setJsonDrafts(computeJsonDrafts(defs, asset.field_values));
        }
      } catch (err) {
        if (cancelled) return;
        setLoadError(
          err instanceof ApiError
            ? err.problem.detail ?? err.problem.title
            : "Unable to reach the server. Please try again.",
        );
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
    // Only ever re-runs on navigating between a different `id` (or
    // create<->edit) — `form` itself is stable across renders (mantine's
    // `useForm` identity), so it's deliberately excluded from the deps list.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, isEdit]);

  const categoryOptions = useMemo(() => flattenForSelect(buildTree(categories)), [categories]);
  const locationOptions = useMemo(() => flattenForSelect(buildTree(locations)), [locations]);
  const projectOptions = useMemo(() => projects.map((p) => ({ value: String(p.id), label: p.name })), [projects]);

  function applySwitch(categoryId: number, defs: CustomFieldDef[]) {
    const keepKeys = new Set(defs.map((d) => d.key));
    const filtered = Object.fromEntries(
      Object.entries(customValuesRef.current).filter(([k]) => keepKeys.has(k)),
    );
    setCustomValues(filtered);
    setJsonDrafts(computeJsonDrafts(defs, filtered));
    setJsonErrors({});
    setCustomFieldErrors({});
    setFieldDefs(defs);
    form.setFieldValue("category", String(categoryId));

    if (!isConsumableTouchedRef.current) {
      const category = categories.find((c) => c.id === categoryId);
      if (category) form.setFieldValue("is_consumable", category.default_is_consumable);
    }
  }

  const handleCategoryChange = async (newIdStr: string | null) => {
    if (!newIdStr) return; // category select is not clearable — see below
    const newId = Number(newIdStr);
    if (form.values.category === newIdStr) return;

    setFieldDefsError(null);
    setFieldDefsLoading(true);
    try {
      const defs = await api.listCategoryFields(newId);
      const newKeys = new Set(defs.map((d) => d.key));
      const droppedLabels = fieldDefs
        .filter((def) => hasEnteredValue(customValuesRef.current[def.key]) && !newKeys.has(def.key))
        .map((def) => def.label);

      if (droppedLabels.length > 0) {
        setPendingSwitch({ categoryId: newId, fieldDefs: defs, droppedLabels });
      } else {
        applySwitch(newId, defs);
      }
    } catch (err) {
      setFieldDefsError(
        err instanceof ApiError
          ? err.problem.detail ?? err.problem.title
          : "Couldn't load this category's custom fields.",
      );
    } finally {
      setFieldDefsLoading(false);
    }
  };

  /** After a new category is created via the inline "+ New" modal: re-fetch
   * the category list, find the one that wasn't there before, and route it
   * through `handleCategoryChange` (rather than setting form state directly)
   * so its custom fields load and all the same side effects fire as a normal
   * category pick. */
  const handleCategoryCreated = async () => {
    const previousIds = new Set(categories.map((c) => c.id));
    const refreshed = await api.listAllCategories({ ordering: "name" });
    setCategories(refreshed);
    const created = refreshed.find((c) => !previousIds.has(c.id));
    if (created) void handleCategoryChange(String(created.id));
  };

  /** Mirrors `handleCategoryCreated` for the inline "+ New" location modal —
   * `Location` has no side effects to reuse, so this sets the form field
   * directly. */
  const handleLocationCreated = async () => {
    const previousIds = new Set(locations.map((l) => l.id));
    const refreshed = await api.listAllLocations({ ordering: "name" });
    setLocations(refreshed);
    const created = refreshed.find((l) => !previousIds.has(l.id));
    if (created) form.setFieldValue("location", String(created.id));
  };

  const handleCustomFieldChange = (key: string, value: unknown) => {
    setCustomValues((prev) => ({ ...prev, [key]: value }));
  };

  const handleJsonDraftChange = (key: string, raw: string) => {
    setJsonDrafts((prev) => ({ ...prev, [key]: raw }));
    if (raw.trim() === "") {
      setCustomValues((prev) => ({ ...prev, [key]: null }));
      setJsonErrors((prev) => {
        const next = { ...prev };
        delete next[key];
        return next;
      });
      return;
    }
    try {
      const parsed = JSON.parse(raw);
      setCustomValues((prev) => ({ ...prev, [key]: parsed }));
      setJsonErrors((prev) => {
        const next = { ...prev };
        delete next[key];
        return next;
      });
    } catch {
      setJsonErrors((prev) => ({ ...prev, [key]: "Invalid JSON." }));
    }
  };

  function validateCustomFields(values: Record<string, unknown>): Record<string, string> {
    const errors: Record<string, string> = {};
    for (const def of fieldDefs) {
      const value = values[def.key];
      const missing = !hasEnteredValue(value);
      if (def.required && missing) {
        errors[def.key] = "This field is required.";
        continue;
      }
      if (missing) continue;

      switch (def.data_type) {
        case "int":
          if (typeof value !== "number" || !Number.isInteger(value)) errors[def.key] = "Expected an integer value.";
          break;
        case "float":
          if (typeof value !== "number" || Number.isNaN(value)) errors[def.key] = "Expected a numeric value.";
          break;
        case "bool":
          if (typeof value !== "boolean") errors[def.key] = "Expected a boolean value.";
          break;
        case "date":
          if (typeof value !== "string" || Number.isNaN(Date.parse(value)))
            errors[def.key] = "Expected a valid date.";
          break;
        case "enum":
          if (!def.enum_options.includes(String(value)))
            errors[def.key] = `Must be one of: ${def.enum_options.join(", ")}.`;
          break;
        case "url":
          if (typeof value !== "string" || !isValidAbsoluteUrl(value))
            errors[def.key] = "Expected a valid URL starting with http:// or https://.";
          break;
        default:
          break;
      }
    }
    return errors;
  }

  /** Step 1 of stock setup: `POST /stock/` for `assetId` only. Returns `null`
   * (no-op) if the unit of measure was left blank (stock setup is optional —
   * a consumable asset can still be created/edited without one, tracking
   * added later). Throws on failure (including the server's "already has a
   * StockItem" 400) so callers can distinguish "nothing was created" from
   * step 2 failing below. */
  async function createStockItemOnly(assetId: number) {
    if (!stockUnit.trim()) return null;
    const payload: StockItemCreatePayload = {
      asset: assetId,
      unit_of_measure: stockUnit.trim(),
      reorder_threshold: stockReorderThreshold === "" ? 0 : stockReorderThreshold,
      reorder_target: stockReorderTarget === "" ? 0 : stockReorderTarget,
    };
    return api.createStockItem(payload);
  }

  /** Step 2 of stock setup: a `receive` txn (Contract 3) to set the entered
   * initial quantity on an already-created `StockItem`. No-op if the user
   * left the initial quantity at 0 (the `StockItem` already starts there).
   * Split out from `createStockItemOnly` so a failure here — after the
   * `StockItem` already exists — can be retried on its own, without
   * re-running (and 400ing on) the create call. */
  async function receiveInitialStock(stockItemId: number) {
    const initialQty = stockInitialQty === "" ? 0 : stockInitialQty;
    if (initialQty > 0) {
      await api.postStockTxn(stockItemId, {
        reason: "receive",
        delta: initialQty,
        ref: "Initial stock (asset setup)",
      });
    }
  }

  /** Edit-mode-only standalone "Set up stock tracking" action — the asset
   * already exists (unlike create mode, there's no single combined submit to
   * piggyback on). */
  const handleSetupStock = async (assetId: number) => {
    setStockSetupError(null);
    setStockSetupSuccess(null);
    setPendingStockReceiveRetry(null);
    if (!stockUnit.trim()) {
      setStockSetupError("Unit of measure is required to set up stock tracking.");
      return;
    }
    setStockSetupBusy(true);
    try {
      const stockItem = await createStockItemOnly(assetId);
      if (!stockItem) return; // unreachable given the blank-unit guard above
      try {
        await receiveInitialStock(stockItem.id);
        setStockAlreadyTracked(true);
        setStockSetupSuccess("Stock tracking set up for this asset.");
      } catch (receiveErr) {
        // The StockItem WAS created (at quantity 0) — this is a partial
        // success, not a total failure, and re-running this handler would
        // now just 400 "already has a StockItem". Offer a scoped retry of
        // just the receive txn instead (`pendingStockReceiveRetry`).
        setStockAlreadyTracked(true);
        setPendingStockReceiveRetry({ stockItemId: stockItem.id });
        setStockSetupError(
          receiveErr instanceof ApiError
            ? `Stock tracking was set up (starting at 0 units), but setting the initial quantity failed: ${
                receiveErr.problem.detail ?? receiveErr.problem.title
              }. You can add stock from the Stock screen, or retry below.`
            : "Stock tracking was set up (starting at 0 units), but setting the initial quantity failed: unable to reach the server. You can add stock from the Stock screen, or retry below.",
        );
      }
    } catch (err) {
      if (err instanceof ApiError && err.problem.errors?.asset) {
        // "Only a consumable asset may own a StockItem" / "already has a
        // StockItem" both land here — the latter means someone else already
        // set this up (e.g. a concurrent edit) mid-session BEFORE this
        // action ran, so there's nothing of this action's own to retry.
        setStockAlreadyTracked(true);
      }
      setStockSetupError(
        err instanceof ApiError
          ? messageFromDetail(err.problem.errors?.asset ?? err.problem.detail ?? err.problem.title)
          : "Unable to reach the server. Please try again.",
      );
    } finally {
      setStockSetupBusy(false);
    }
  };

  /** Retry just the "receive" txn for a `StockItem` this form already
   * created in this session (`pendingStockReceiveRetry`) — recovery path for
   * the "StockItem created, receive txn failed" case above. */
  const handleRetryStockReceive = async () => {
    if (!pendingStockReceiveRetry) return;
    setStockSetupBusy(true);
    setStockSetupError(null);
    try {
      await receiveInitialStock(pendingStockReceiveRetry.stockItemId);
      setPendingStockReceiveRetry(null);
      setStockSetupSuccess("Initial quantity set for this asset's stock.");
    } catch (err) {
      setStockSetupError(
        err instanceof ApiError
          ? `Setting the initial quantity failed: ${
              err.problem.detail ?? err.problem.title
            }. You can add stock from the Stock screen, or retry below.`
          : "Unable to reach the server. Please try again.",
      );
    } finally {
      setStockSetupBusy(false);
    }
  };

  function applyServerErrors(errors: Record<string, unknown>) {
    const standardErrors: Record<string, string> = {};
    const cfErrors: Record<string, string> = {};
    for (const [key, value] of Object.entries(errors)) {
      if (key === "custom_field_values" && typeof value === "object" && value !== null && !Array.isArray(value)) {
        for (const [ck, cv] of Object.entries(value as Record<string, unknown>)) {
          cfErrors[ck] = messageFromDetail(cv);
        }
      } else {
        standardErrors[key] = messageFromDetail(value);
      }
    }
    form.setErrors(standardErrors);
    setCustomFieldErrors(cfErrors);
  }

  const handleSubmit = form.onSubmit(async (values) => {
    setSubmitError(null);

    if (Object.keys(jsonErrors).length > 0) {
      setSubmitError("Fix the invalid JSON field(s) below before submitting.");
      return;
    }

    const keepKeys = new Set(fieldDefs.map((d) => d.key));
    const cleanedCustomValues = Object.fromEntries(
      Object.entries(customValues).filter(([k]) => keepKeys.has(k)),
    );

    const cfErrors = validateCustomFields(cleanedCustomValues);
    setCustomFieldErrors(cfErrors);
    if (Object.keys(cfErrors).length > 0) {
      setSubmitError("Fix the highlighted field(s) below before submitting.");
      return;
    }

    const payload: AssetWritePayload = {
      category: Number(values.category),
      name: values.name.trim(),
      description: values.description,
      is_consumable: values.is_consumable,
      project: values.project ? Number(values.project) : null,
      serial_number: values.serial_number,
      manufacturer: values.manufacturer,
      model: values.model,
      location: values.location ? Number(values.location) : null,
      purchase_date: values.purchase_date || null,
      purchase_cost: values.purchase_cost === "" ? null : String(values.purchase_cost),
      currency: values.currency,
      warranty_expiry: values.warranty_expiry || null,
      supplier: values.supplier,
      url: values.url.trim(),
      status: values.status,
      condition: values.condition,
      tags: values.tags,
      custom_field_values: cleanedCustomValues,
    };

    setSubmitting(true);
    try {
      const saved =
        isEdit && existingAsset
          ? await api.updateAsset(existingAsset.id, payload)
          : await api.createAsset(payload);

      // Feature C: only on CREATE — edit mode has its own standalone "Set up
      // stock tracking" button (`handleSetupStock`) since the asset already
      // existed before this submit. The asset itself already saved
      // successfully at this point, so a stock-setup failure must not read
      // as "your changes were lost" — it's surfaced as a banner on the asset
      // detail page the caller lands on regardless (`location.state`, see
      // `AssetDetailScreen`), not as a blocking error on this form.
      let stockWarning: string | null = null;
      let stockRetry: { stockItemId: number; initialQty: number } | null = null;
      if (!isEdit && values.is_consumable) {
        try {
          const stockItem = await createStockItemOnly(saved.id);
          if (stockItem) {
            try {
              await receiveInitialStock(stockItem.id);
            } catch (receiveErr) {
              // The StockItem WAS created (at quantity 0) — this is a
              // partial success, not "stock setup failed" outright, and the
              // Asset Detail screen this navigate lands on gets a scoped
              // retry it can offer against the known `stockItem.id`.
              stockWarning =
                receiveErr instanceof ApiError
                  ? `Stock tracking was set up (starting at 0 units), but setting the initial quantity failed: ${
                      receiveErr.problem.detail ?? receiveErr.problem.title
                    }. You can add stock from the Stock screen.`
                  : "Stock tracking was set up (starting at 0 units), but setting the initial quantity failed: unable to reach the server. You can add stock from the Stock screen.";
              stockRetry = { stockItemId: stockItem.id, initialQty: stockInitialQty === "" ? 0 : stockInitialQty };
            }
          }
        } catch (stockErr) {
          stockWarning =
            stockErr instanceof ApiError
              ? `Asset created, but stock setup failed: ${stockErr.problem.detail ?? stockErr.problem.title}`
              : "Asset created, but stock setup failed: unable to reach the server.";
        }
      }
      navigate(
        `/assets/${saved.id}`,
        stockWarning ? { state: { banner: stockWarning, stockRetry } } : undefined,
      );
    } catch (err) {
      if (err instanceof ApiError && err.problem.errors) {
        applyServerErrors(err.problem.errors);
        setSubmitError(err.problem.detail ?? "Please fix the highlighted field(s) and try again.");
      } else if (err instanceof ApiError && err.isForbidden) {
        // A server 403 here is a normal, handled outcome (CLAUDE.md) — the
        // client's own entry-point gate below can drift from the server's
        // real scoped check (e.g. a membership changed mid-session).
        setSubmitError("You don't have permission to save this asset.");
      } else if (err instanceof ApiError) {
        setSubmitError(err.problem.detail ?? err.problem.title);
      } else {
        setSubmitError("Unable to reach the server. Please try again.");
      }
    } finally {
      setSubmitting(false);
    }
  });

  const handlePhotoUpload = async (file: File | null) => {
    if (!file || !existingAsset) return;
    setUploading(true);
    setUploadError(null);
    try {
      const attachment = await api.uploadAssetAttachment(existingAsset.id, file, "photo");
      setExistingAsset((prev) => (prev ? { ...prev, attachments: [attachment, ...prev.attachments] } : prev));
    } catch (err) {
      setUploadError(
        err instanceof ApiError ? err.problem.detail ?? err.problem.title : "Upload failed. Please try again.",
      );
    } finally {
      setUploading(false);
      resetFileRef.current?.();
    }
  };

  if (loading) {
    return (
      <AppLayout title={isEdit ? "Edit asset" : "New asset"} backTo="/assets">
        <Center h="60vh">
          <Loader data-testid="asset-form-loading" />
        </Center>
      </AppLayout>
    );
  }

  if (loadError || (isEdit && !existingAsset)) {
    return (
      <AppLayout title={isEdit ? "Edit asset" : "New asset"} backTo="/assets">
        <Center h="60vh" p="md">
          <Stack align="center" gap="sm" maw={420}>
            <Alert color="red" title="Couldn't load this form" data-testid="asset-form-load-error" w="100%">
              {loadError ?? "Asset not found."}
            </Alert>
            <Button onClick={() => navigate("/assets")}>Back to Assets</Button>
          </Stack>
        </Center>
      </AppLayout>
    );
  }

  // Presentation-only entry-point gating (CLAUDE.md/rbac.md §1): the server
  // re-checks `asset.create`/`asset.edit` for real on submit regardless —
  // this only avoids showing a form the user will just get 403'd on.
  const canSubmit = isEdit
    ? existingAsset
      ? hasAssetPermission(me, ASSET_EDIT, existingAsset.project)
      : false
    : hasAnyAssetPermission(me, ASSET_CREATE);

  if (!canSubmit) {
    return (
      <AppLayout title={isEdit ? "Edit asset" : "New asset"} backTo="/assets">
        <Center h="60vh" p="md">
          <Stack align="center" gap="sm" maw={420}>
            <Alert color="yellow" title="No permission" data-testid="asset-form-forbidden" w="100%">
              You don&apos;t have permission to {isEdit ? "edit this asset" : "create a new asset"}.
            </Alert>
            <Button onClick={() => navigate(isEdit && existingAsset ? `/assets/${existingAsset.id}` : "/assets")}>
              Back
            </Button>
          </Stack>
        </Center>
      </AppLayout>
    );
  }

  const canAttach = existingAsset ? hasAssetPermission(me, ASSET_ATTACH, existingAsset.project) : false;
  const stockProjectId = existingAsset ? existingAsset.project : form.values.project ? Number(form.values.project) : null;
  const canManageStock = hasAssetPermission(me, STOCK_ADJUST, stockProjectId);

  return (
    <AppLayout
      title={isEdit ? `Edit ${existingAsset?.name ?? "asset"}` : "New asset"}
      backTo={isEdit && existingAsset ? `/assets/${existingAsset.id}` : "/assets"}
    >
        <form onSubmit={handleSubmit} noValidate>
          <Stack gap="md" pb="xl" maw={640}>
            {submitError && (
              <Alert color="red" data-testid="asset-form-error">
                {submitError}
              </Alert>
            )}

            <Card withBorder>
              <Stack gap="sm">
                <Title order={6}>Identity</Title>
                <Group align="flex-end" gap="xs">
                  <Select
                    flex={1}
                    label="Category"
                    placeholder="Select a category"
                    data={categoryOptions}
                    value={form.values.category}
                    onChange={(v) => void handleCategoryChange(v)}
                    error={form.errors.category}
                    searchable
                    required
                    disabled={fieldDefsLoading}
                    rightSection={fieldDefsLoading ? <Loader size="xs" /> : undefined}
                    data-testid="asset-form-category"
                  />
                  {hasPermission(me, CATEGORY_MANAGE) && (
                    <Button
                      variant="light"
                      size="sm"
                      onClick={() => setCategoryModalOpen(true)}
                      data-testid="asset-form-new-category"
                    >
                      + New
                    </Button>
                  )}
                </Group>
                {fieldDefsError && (
                  <Alert color="red" data-testid="asset-form-field-defs-error">
                    {fieldDefsError}
                  </Alert>
                )}
                <TextInput label="Name" required {...form.getInputProps("name")} data-testid="asset-form-name" />
                <Textarea label="Description" autosize minRows={2} {...form.getInputProps("description")} />
                <Switch
                  label="Consumable"
                  description="Tracked by quantity (stock) rather than as an individual durable item"
                  checked={form.values.is_consumable}
                  onChange={(e) => {
                    isConsumableTouchedRef.current = true;
                    form.setFieldValue("is_consumable", e.currentTarget.checked);
                  }}
                />
                <TagsInput
                  label="Tags"
                  placeholder="Type a tag and press Enter"
                  {...form.getInputProps("tags")}
                />
              </Stack>
            </Card>

            {form.values.is_consumable && (
              <Card withBorder data-testid="asset-form-stock-card">
                <Stack gap="sm">
                  <Title order={6}>Consumable stock</Title>
                  {!canManageStock ? (
                    <Text size="sm" c="dimmed">
                      You don&apos;t have permission to set up stock tracking for this asset.
                    </Text>
                  ) : stockAlreadyTracked ? (
                    pendingStockReceiveRetry ? (
                      <Stack gap="xs">
                        {stockSetupError && (
                          <Alert color="yellow" data-testid="asset-form-stock-error">
                            {stockSetupError}
                          </Alert>
                        )}
                        <Group justify="flex-end">
                          <Button
                            size="xs"
                            variant="light"
                            loading={stockSetupBusy}
                            onClick={() => void handleRetryStockReceive()}
                            data-testid="asset-form-stock-retry"
                          >
                            Retry setting initial quantity
                          </Button>
                        </Group>
                      </Stack>
                    ) : (
                      <Text size="sm" c="teal">
                        {stockSetupSuccess ?? "Stock tracking is already set up for this asset"} — manage quantities
                        from the Stock screen.
                      </Text>
                    )
                  ) : (
                    <>
                      <Text size="xs" c="dimmed">
                        {isEdit
                          ? "Add stock tracking to this existing consumable asset."
                          : "Optional — leave the unit blank to add stock tracking later from the edit screen."}
                      </Text>
                      {stockSetupError && (
                        <Alert color="red" data-testid="asset-form-stock-error">
                          {stockSetupError}
                        </Alert>
                      )}
                      <TextInput
                        label="Unit of measure"
                        placeholder="e.g. pcs, ml, rolls"
                        value={stockUnit}
                        onChange={(e) => setStockUnit(e.currentTarget.value)}
                        data-testid="asset-form-stock-unit"
                      />
                      <SimpleGrid cols={{ base: 1, sm: 3 }} spacing="sm">
                        <NumberInput
                          label="Initial quantity"
                          min={0}
                          value={stockInitialQty}
                          onChange={(v) => setStockInitialQty(v === "" ? "" : Number(v))}
                        />
                        <NumberInput
                          label="Reorder threshold"
                          min={0}
                          value={stockReorderThreshold}
                          onChange={(v) => setStockReorderThreshold(v === "" ? "" : Number(v))}
                        />
                        <NumberInput
                          label="Reorder target"
                          min={0}
                          value={stockReorderTarget}
                          onChange={(v) => setStockReorderTarget(v === "" ? "" : Number(v))}
                        />
                      </SimpleGrid>
                      {isEdit && existingAsset && (
                        <Group justify="flex-end">
                          <Button
                            size="xs"
                            variant="light"
                            loading={stockSetupBusy}
                            onClick={() => void handleSetupStock(existingAsset.id)}
                            data-testid="asset-form-stock-setup"
                          >
                            Set up stock tracking
                          </Button>
                        </Group>
                      )}
                    </>
                  )}
                </Stack>
              </Card>
            )}

            <Card withBorder>
              <Stack gap="sm">
                <Title order={6}>Custom fields</Title>
                {form.values.category ? (
                  fieldDefs.length === 0 ? (
                    <Text size="sm" c="dimmed">
                      This category has no custom fields defined.
                    </Text>
                  ) : (
                    <CustomFieldInputs
                      fieldDefs={fieldDefs}
                      values={customValues}
                      onChange={handleCustomFieldChange}
                      jsonDrafts={jsonDrafts}
                      onJsonDraftChange={handleJsonDraftChange}
                      jsonErrors={jsonErrors}
                      errors={customFieldErrors}
                    />
                  )
                ) : (
                  <Text size="sm" c="dimmed">
                    Select a category to see its custom fields.
                  </Text>
                )}
              </Stack>
            </Card>

            <Card withBorder>
              <Stack gap="sm">
                <Title order={6}>Location &amp; project</Title>
                <Group align="flex-end" gap="xs">
                  <Select
                    flex={1}
                    label="Location"
                    placeholder="No location"
                    data={locationOptions}
                    clearable
                    searchable
                    {...form.getInputProps("location")}
                  />
                  {hasPermission(me, LOCATION_MANAGE) && (
                    <Button
                      variant="light"
                      size="sm"
                      onClick={() => setLocationModalOpen(true)}
                      data-testid="asset-form-new-location"
                    >
                      + New
                    </Button>
                  )}
                </Group>
                <Select
                  label="Project"
                  placeholder="General pool"
                  data={projectOptions}
                  clearable
                  searchable
                  {...form.getInputProps("project")}
                />
                <Select
                  label="Status"
                  data={STATUS_SELECT_OPTIONS}
                  allowDeselect={false}
                  {...form.getInputProps("status")}
                />
                <Textarea label="Condition notes" autosize minRows={2} {...form.getInputProps("condition")} />
              </Stack>
            </Card>

            <Card withBorder>
              <Stack gap="sm">
                <Title order={6}>Physical &amp; commercial</Title>
                <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="sm">
                  <TextInput label="Serial number" {...form.getInputProps("serial_number")} />
                  <TextInput label="Manufacturer" {...form.getInputProps("manufacturer")} />
                  <TextInput label="Model" {...form.getInputProps("model")} />
                  <TextInput label="Supplier" {...form.getInputProps("supplier")} />
                  <TextInput
                    label="Link"
                    description="Product, procurement, or documentation page"
                    placeholder="https://…"
                    inputMode="url"
                    {...form.getInputProps("url")}
                  />
                  <TextInput type="date" label="Purchase date" {...form.getInputProps("purchase_date")} />
                  <TextInput type="date" label="Warranty expiry" {...form.getInputProps("warranty_expiry")} />
                  <NumberInput
                    label="Purchase cost"
                    decimalScale={2}
                    {...form.getInputProps("purchase_cost")}
                  />
                  <TextInput
                    label="Currency"
                    placeholder="USD"
                    maxLength={3}
                    {...form.getInputProps("currency")}
                    error={form.errors.currency}
                  />
                </SimpleGrid>
              </Stack>
            </Card>

            {isEdit && existingAsset && (
              <Card withBorder>
                <Stack gap="sm">
                  <Group justify="space-between">
                    <Title order={6}>Photos &amp; attachments</Title>
                    {canAttach ? (
                      <FileButton
                        resetRef={resetFileRef}
                        onChange={(file) => void handlePhotoUpload(file)}
                        accept="image/png,image/jpeg,image/webp"
                      >
                        {(props) => (
                          <Button size="xs" variant="light" loading={uploading} {...props}>
                            Add photo
                          </Button>
                        )}
                      </FileButton>
                    ) : (
                      <Text size="xs" c="dimmed">
                        No permission to attach files
                      </Text>
                    )}
                  </Group>
                  {uploadError && <Alert color="red">{uploadError}</Alert>}
                  {existingAsset.attachments.filter((a) => a.kind === "photo").length > 0 && (
                    <SimpleGrid cols={{ base: 2, sm: 4 }} spacing="xs">
                      {existingAsset.attachments
                        .filter((a) => a.kind === "photo")
                        .map((att) => (
                          <Image
                            key={att.id}
                            src={`/media/${att.storage_key}`}
                            alt={att.filename}
                            radius="sm"
                            fit="cover"
                            h={80}
                          />
                        ))}
                    </SimpleGrid>
                  )}
                </Stack>
              </Card>
            )}

            {!isEdit && (
              <Text size="xs" c="dimmed">
                You can add photos once the asset is created.
              </Text>
            )}

            <Group justify="flex-end">
              <Button
                variant="default"
                onClick={() => navigate(isEdit && existingAsset ? `/assets/${existingAsset.id}` : "/assets")}
              >
                Cancel
              </Button>
              <Button type="submit" loading={submitting} data-testid="asset-form-submit">
                {isEdit ? "Save changes" : "Create asset"}
              </Button>
            </Group>
          </Stack>
        </form>

      <Modal
        opened={pendingSwitch !== null}
        onClose={() => setPendingSwitch(null)}
        title="Switch category?"
        centered
      >
        <Text size="sm" mb="md">
          Switching category will clear the following entered value(s), which don&apos;t exist on the new
          category:
        </Text>
        <Stack gap={2} mb="md">
          {pendingSwitch?.droppedLabels.map((label) => (
            <Text key={label} size="sm" fw={600}>
              &bull; {label}
            </Text>
          ))}
        </Stack>
        <Group justify="flex-end">
          <Button variant="default" onClick={() => setPendingSwitch(null)}>
            Cancel
          </Button>
          <Button
            color="red"
            onClick={() => {
              if (pendingSwitch) applySwitch(pendingSwitch.categoryId, pendingSwitch.fieldDefs);
              setPendingSwitch(null);
            }}
          >
            Switch &amp; clear
          </Button>
        </Group>
      </Modal>

      <CategoryFormModal
        opened={categoryModalOpen}
        onClose={() => setCategoryModalOpen(false)}
        onSaved={() => void handleCategoryCreated()}
        tree={buildTree(categories)}
        editing={null}
        presetParentId={null}
      />

      <LocationFormModal
        opened={locationModalOpen}
        onClose={() => setLocationModalOpen(false)}
        onSaved={() => void handleLocationCreated()}
        tree={buildTree(locations)}
        editing={null}
        presetParentId={null}
      />
    </AppLayout>
  );
}
