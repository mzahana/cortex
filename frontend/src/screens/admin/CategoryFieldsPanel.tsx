import { useEffect, useState } from "react";
import {
  ActionIcon,
  Alert,
  Badge,
  Button,
  Checkbox,
  Group,
  Modal,
  NumberInput,
  Paper,
  Select,
  Stack,
  Table,
  TagsInput,
  Text,
  TextInput,
  Title,
  Tooltip,
} from "@mantine/core";
import { useForm } from "@mantine/form";
import { api, ApiError } from "../../api/client";
import { ConfirmDeleteModal } from "../../components/ConfirmDeleteModal";
import type {
  Category,
  CustomFieldDataType,
  CustomFieldDef,
  CustomFieldDefCreatePayload,
  CustomFieldDefUpdatePayload,
} from "../../api/types";

const DATA_TYPES: { value: CustomFieldDataType; label: string }[] = [
  { value: "text", label: "Text" },
  { value: "int", label: "Integer" },
  { value: "float", label: "Float" },
  { value: "bool", label: "Boolean" },
  { value: "date", label: "Date" },
  { value: "enum", label: "Enum" },
  { value: "json", label: "JSON" },
];

interface FieldFormValues {
  key: string;
  label: string;
  data_type: CustomFieldDataType;
  unit: string;
  enum_options: string[];
  required: boolean;
  order: number;
}

interface CategoryFieldsPanelProps {
  category: Category;
  canManage: boolean;
  /** Called after a field is successfully added/edited/deleted/reordered so
   * the parent can refetch the category list (field_defs are embedded on
   * `Category`, per `CategorySerializer`, so there is no separate list to
   * invalidate). */
  onFieldAdded: () => void;
}

/**
 * Manages the `CustomFieldDef` list for one category (T1.5 + M1 follow-up:
 * edit/delete/reorder). Add is an inline form at the bottom; edit is a
 * modal reusing the same field shape; delete is confirmed via
 * `ConfirmDeleteModal` with an explicit cascade warning (deleting a field
 * def deletes every stored `AssetFieldValue` for it, on every asset); reorder
 * is up/down controls (no drag-and-drop dependency in the project, and
 * up/down is simpler to use one-handed on a phone) that submit the full
 * ordered id set to `POST .../fields/reorder`.
 *
 * All three write actions are gated behind `canManage` (presentation only —
 * the server is the real authority and a 403 is handled the same as any
 * other `ApiError`, never assumed to be a bug).
 */
export function CategoryFieldsPanel({ category, canManage, onFieldAdded }: CategoryFieldsPanelProps) {
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const [editing, setEditing] = useState<CustomFieldDef | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<CustomFieldDef | null>(null);
  const [reorderError, setReorderError] = useState<string | null>(null);
  const [reorderingId, setReorderingId] = useState<number | null>(null);

  const form = useForm<FieldFormValues>({
    initialValues: {
      key: "",
      label: "",
      data_type: "text",
      unit: "",
      enum_options: [],
      required: false,
      order: category.field_defs.length,
    },
    validate: {
      key: (value) =>
        /^[a-z0-9_-]+$/.test(value) ? null : "Lowercase letters, numbers, hyphens, underscores only",
      label: (value) => (value.trim() ? null : "Label is required"),
      enum_options: (value, values) =>
        values.data_type === "enum" && value.length === 0
          ? "At least one option is required for an enum field"
          : null,
    },
  });

  const sortedFieldDefs = [...category.field_defs].sort((a, b) => a.order - b.order || a.id - b.id);

  const handleSubmit = async (values: FieldFormValues) => {
    setFormError(null);
    setSubmitting(true);
    try {
      const payload: CustomFieldDefCreatePayload = {
        key: values.key,
        label: values.label,
        data_type: values.data_type,
        unit: values.unit || undefined,
        enum_options: values.data_type === "enum" ? values.enum_options : undefined,
        required: values.required,
        order: values.order,
      };
      await api.createCategoryField(category.id, payload);
      form.reset();
      form.setFieldValue("order", category.field_defs.length + 1);
      onFieldAdded();
    } catch (err) {
      if (err instanceof ApiError && err.problem.errors) {
        form.setErrors(
          Object.fromEntries(
            Object.entries(err.problem.errors).map(([k, v]) => [k, Array.isArray(v) ? v.join(" ") : String(v)]),
          ),
        );
      } else if (err instanceof ApiError) {
        setFormError(err.problem.detail ?? err.problem.title);
      } else {
        setFormError("Unable to reach the server. Please try again.");
      }
    } finally {
      setSubmitting(false);
    }
  };

  /** Moves `fieldDef` one position up/down in `sortedFieldDefs` and submits
   * the FULL resulting id set to `POST .../fields/reorder` — the endpoint
   * requires exactly the category's current field ids, so a partial list is
   * never sent. */
  const move = async (fieldDef: CustomFieldDef, direction: -1 | 1) => {
    setReorderError(null);
    const index = sortedFieldDefs.findIndex((fd) => fd.id === fieldDef.id);
    const targetIndex = index + direction;
    if (index < 0 || targetIndex < 0 || targetIndex >= sortedFieldDefs.length) return;

    const reordered = [...sortedFieldDefs];
    [reordered[index], reordered[targetIndex]] = [reordered[targetIndex], reordered[index]];

    setReorderingId(fieldDef.id);
    try {
      await api.reorderCategoryFields(
        category.id,
        reordered.map((fd) => fd.id),
      );
      onFieldAdded();
    } catch (err) {
      if (err instanceof ApiError) {
        setReorderError(err.problem.detail ?? err.problem.title);
      } else {
        setReorderError("Unable to reach the server. Please try again.");
      }
    } finally {
      setReorderingId(null);
    }
  };

  return (
    <Paper withBorder p="md" radius="md">
      <Stack gap="sm">
        <Title order={4}>Custom fields — {category.name}</Title>

        {reorderError && (
          <Alert color="red" data-testid="reorder-error" onClose={() => setReorderError(null)} withCloseButton>
            {reorderError}
          </Alert>
        )}

        {sortedFieldDefs.length === 0 ? (
          <Text c="dimmed" size="sm">
            No custom fields defined yet.
          </Text>
        ) : (
          <Table striped verticalSpacing="xs" style={{ tableLayout: "fixed" }}>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Key</Table.Th>
                <Table.Th>Label</Table.Th>
                <Table.Th>Type</Table.Th>
                <Table.Th>Unit</Table.Th>
                <Table.Th>Required</Table.Th>
                <Table.Th>Order</Table.Th>
                {canManage && <Table.Th>Actions</Table.Th>}
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {sortedFieldDefs.map((fd, i) => (
                <Table.Tr key={fd.id}>
                  <Table.Td>
                    <Text ff="monospace" size="sm">
                      {fd.key}
                    </Text>
                  </Table.Td>
                  <Table.Td>{fd.label}</Table.Td>
                  <Table.Td>
                    <Badge variant="light">{fd.data_type}</Badge>
                    {fd.data_type === "enum" && fd.enum_options.length > 0 && (
                      <Text size="xs" c="dimmed">
                        {fd.enum_options.join(", ")}
                      </Text>
                    )}
                  </Table.Td>
                  <Table.Td>{fd.unit || "—"}</Table.Td>
                  <Table.Td>{fd.required ? "Yes" : "No"}</Table.Td>
                  <Table.Td>{fd.order}</Table.Td>
                  {canManage && (
                    <Table.Td>
                      <Group gap={4} wrap="nowrap">
                        <Tooltip label="Move up">
                          <ActionIcon
                            variant="subtle"
                            size="sm"
                            aria-label={`Move ${fd.label} up`}
                            disabled={i === 0 || reorderingId !== null}
                            loading={reorderingId === fd.id}
                            onClick={() => void move(fd, -1)}
                          >
                            ↑
                          </ActionIcon>
                        </Tooltip>
                        <Tooltip label="Move down">
                          <ActionIcon
                            variant="subtle"
                            size="sm"
                            aria-label={`Move ${fd.label} down`}
                            disabled={i === sortedFieldDefs.length - 1 || reorderingId !== null}
                            loading={reorderingId === fd.id}
                            onClick={() => void move(fd, 1)}
                          >
                            ↓
                          </ActionIcon>
                        </Tooltip>
                        <Tooltip label="Edit">
                          <ActionIcon
                            variant="subtle"
                            size="sm"
                            aria-label={`Edit ${fd.label}`}
                            onClick={() => setEditing(fd)}
                          >
                            ✎
                          </ActionIcon>
                        </Tooltip>
                        <Tooltip label="Delete">
                          <ActionIcon
                            variant="subtle"
                            size="sm"
                            color="red"
                            aria-label={`Delete ${fd.label}`}
                            onClick={() => setDeleteTarget(fd)}
                          >
                            🗑
                          </ActionIcon>
                        </Tooltip>
                      </Group>
                    </Table.Td>
                  )}
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        )}

        {!canManage && (
          <Text c="dimmed" size="xs">
            You have read-only access to this category&apos;s fields.
          </Text>
        )}

        {canManage && (
          <form onSubmit={form.onSubmit(handleSubmit)} noValidate>
            <Stack gap="xs">
              <Title order={5}>Add field</Title>
              {formError && (
                <Alert color="red" data-testid="field-form-error">
                  {formError}
                </Alert>
              )}
              <Group grow>
                <TextInput
                  label="Key"
                  placeholder="vram_gb"
                  description="Machine key"
                  required
                  {...form.getInputProps("key")}
                />
                <TextInput label="Label" placeholder="VRAM" required {...form.getInputProps("label")} />
              </Group>
              <Group grow>
                <Select
                  label="Type"
                  data={DATA_TYPES}
                  allowDeselect={false}
                  {...form.getInputProps("data_type")}
                />
                <TextInput label="Unit" placeholder="GB" {...form.getInputProps("unit")} />
                <NumberInput label="Order" min={0} {...form.getInputProps("order")} />
              </Group>
              {form.values.data_type === "enum" && <TagsInputForEnum form={form} />}
              <Checkbox label="Required" {...form.getInputProps("required", { type: "checkbox" })} />
              <Button type="submit" loading={submitting}>
                Add field
              </Button>
            </Stack>
          </form>
        )}
      </Stack>

      {editing && (
        <EditFieldModal
          category={category}
          fieldDef={editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            onFieldAdded();
          }}
        />
      )}

      {deleteTarget && (
        <ConfirmDeleteModal
          opened={!!deleteTarget}
          title="Delete custom field"
          itemLabel={deleteTarget.label}
          onClose={() => setDeleteTarget(null)}
          onConfirm={async () => {
            await api.deleteCategoryField(category.id, deleteTarget.id);
          }}
          onDeleted={onFieldAdded}
        >
          <Alert color="red" variant="light" mb="sm" data-testid="delete-field-cascade-warning">
            <strong>This is destructive and cannot be undone.</strong> Deleting this field
            removes it AND every value already stored for it on every asset in this
            category (cascading delete at the database layer) — not just the field
            definition.
          </Alert>
        </ConfirmDeleteModal>
      )}
    </Paper>
  );
}

function TagsInputForEnum({ form }: { form: ReturnType<typeof useForm<FieldFormValues>> }) {
  return (
    <TagsInput
      label="Enum options"
      placeholder="Type a value and press Enter"
      description="Allowed values for this enum field"
      {...form.getInputProps("enum_options")}
    />
  );
}

interface EditFieldModalProps {
  category: Category;
  fieldDef: CustomFieldDef;
  onClose: () => void;
  onSaved: () => void;
}

/** Edit modal for a single existing `CustomFieldDef` (M1 follow-up). Reuses
 * the same field shape as the add form. The server is the sole authority on
 * whether `data_type` can change (blocked with a `400` once the field has
 * stored `AssetFieldValue` rows) — the client has no cheap way to know that
 * in advance (the list payload doesn't carry a "has values" flag), so
 * `data_type` stays editable here and the resulting `400` is surfaced
 * inline on that field, same as any other validation error. */
function EditFieldModal({ category, fieldDef, onClose, onSaved }: EditFieldModalProps) {
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const form = useForm<FieldFormValues>({
    initialValues: {
      key: fieldDef.key,
      label: fieldDef.label,
      data_type: fieldDef.data_type,
      unit: fieldDef.unit,
      enum_options: fieldDef.enum_options,
      required: fieldDef.required,
      order: fieldDef.order,
    },
    validate: {
      key: (value) =>
        /^[a-z0-9_-]+$/.test(value) ? null : "Lowercase letters, numbers, hyphens, underscores only",
      label: (value) => (value.trim() ? null : "Label is required"),
      enum_options: (value, values) =>
        values.data_type === "enum" && value.length === 0
          ? "At least one option is required for an enum field"
          : null,
    },
  });

  // Reset the form whenever a different field def is opened for editing.
  useEffect(() => {
    form.setValues({
      key: fieldDef.key,
      label: fieldDef.label,
      data_type: fieldDef.data_type,
      unit: fieldDef.unit,
      enum_options: fieldDef.enum_options,
      required: fieldDef.required,
      order: fieldDef.order,
    });
    setFormError(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fieldDef.id]);

  const handleSubmit = async (values: FieldFormValues) => {
    setFormError(null);
    setSubmitting(true);
    try {
      const payload: CustomFieldDefUpdatePayload = {
        key: values.key,
        label: values.label,
        data_type: values.data_type,
        unit: values.unit || "",
        enum_options: values.data_type === "enum" ? values.enum_options : [],
        required: values.required,
        order: values.order,
      };
      await api.updateCategoryField(category.id, fieldDef.id, payload);
      onSaved();
    } catch (err) {
      if (err instanceof ApiError && err.problem.errors) {
        form.setErrors(
          Object.fromEntries(
            Object.entries(err.problem.errors).map(([k, v]) => [k, Array.isArray(v) ? v.join(" ") : String(v)]),
          ),
        );
      } else if (err instanceof ApiError) {
        setFormError(err.problem.detail ?? err.problem.title);
      } else {
        setFormError("Unable to reach the server. Please try again.");
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal opened onClose={onClose} title={`Edit field — ${fieldDef.label}`} centered>
      <form onSubmit={form.onSubmit(handleSubmit)} noValidate>
        <Stack gap="xs">
          {formError && (
            <Alert color="red" data-testid="edit-field-form-error">
              {formError}
            </Alert>
          )}
          <Group grow>
            <TextInput label="Key" description="Machine key" required {...form.getInputProps("key")} />
            <TextInput label="Label" required {...form.getInputProps("label")} />
          </Group>
          <Group grow>
            <Select
              label="Type"
              data={DATA_TYPES}
              allowDeselect={false}
              description="Blocked with an error below if this field already has stored values on any asset"
              {...form.getInputProps("data_type")}
            />
            <TextInput label="Unit" {...form.getInputProps("unit")} />
            <NumberInput label="Order" min={0} {...form.getInputProps("order")} />
          </Group>
          {form.values.data_type === "enum" && <TagsInputForEnum form={form} />}
          <Checkbox label="Required" {...form.getInputProps("required", { type: "checkbox" })} />
          <Group justify="flex-end">
            <Button variant="default" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" loading={submitting}>
              Save changes
            </Button>
          </Group>
        </Stack>
      </form>
    </Modal>
  );
}
