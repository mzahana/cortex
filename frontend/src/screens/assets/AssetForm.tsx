import { useEffect, useMemo, useRef, useState } from "react";
import {
  ActionIcon,
  Alert,
  AppShell,
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
import { useNavigate, useParams } from "react-router-dom";
import { api, ApiError } from "../../api/client";
import {
  ASSET_ATTACH,
  ASSET_CREATE,
  ASSET_EDIT,
  hasAnyAssetPermission,
  hasAssetPermission,
} from "../../api/permissions";
import { useAuth } from "../../hooks/useAuth";
import type {
  Asset,
  AssetStatus,
  AssetWritePayload,
  Category,
  CustomFieldDef,
  Location,
  Project,
} from "../../api/types";
import { buildTree, flattenForSelect } from "../../components/treeUtils";
import { STATUS_OPTIONS } from "./assetConstants";
import { CustomFieldInputs } from "./CustomFieldInputs";

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

  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const resetFileRef = useRef<() => void>(null);

  const form = useForm<FormValues>({
    initialValues: EMPTY_VALUES,
    validate: {
      name: (v) => (v.trim() ? null : "Name is required."),
      category: (v) => (v ? null : "Category is required."),
      currency: (v) => (v && v.length > 3 ? "Use a 3-letter currency code (e.g. USD)." : null),
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
        default:
          break;
      }
    }
    return errors;
  }

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
      navigate(`/assets/${saved.id}`);
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
      <Center h="100vh">
        <Loader data-testid="asset-form-loading" />
      </Center>
    );
  }

  if (loadError || (isEdit && !existingAsset)) {
    return (
      <Center h="100vh" p="md">
        <Stack align="center" gap="sm" maw={420}>
          <Alert color="red" title="Couldn't load this form" data-testid="asset-form-load-error" w="100%">
            {loadError ?? "Asset not found."}
          </Alert>
          <Button onClick={() => navigate("/assets")}>Back to Assets</Button>
        </Stack>
      </Center>
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
      <Center h="100vh" p="md">
        <Stack align="center" gap="sm" maw={420}>
          <Alert color="yellow" title="No permission" data-testid="asset-form-forbidden" w="100%">
            You don&apos;t have permission to {isEdit ? "edit this asset" : "create a new asset"}.
          </Alert>
          <Button onClick={() => navigate(isEdit && existingAsset ? `/assets/${existingAsset.id}` : "/assets")}>
            Back
          </Button>
        </Stack>
      </Center>
    );
  }

  const canAttach = existingAsset ? hasAssetPermission(me, ASSET_ATTACH, existingAsset.project) : false;

  return (
    <AppShell header={{ height: 60 }} padding="md">
      <AppShell.Header>
        <Group h="100%" px="md" gap="xs" wrap="nowrap">
          <ActionIcon
            variant="subtle"
            aria-label="Back"
            onClick={() => navigate(isEdit && existingAsset ? `/assets/${existingAsset.id}` : "/assets")}
          >
            &#8592;
          </ActionIcon>
          <Title order={4} lineClamp={1}>
            {isEdit ? `Edit ${existingAsset?.name ?? "asset"}` : "New asset"}
          </Title>
        </Group>
      </AppShell.Header>

      <AppShell.Main>
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
                <Select
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
                <Select
                  label="Location"
                  placeholder="No location"
                  data={locationOptions}
                  clearable
                  searchable
                  {...form.getInputProps("location")}
                />
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
      </AppShell.Main>

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
    </AppShell>
  );
}
