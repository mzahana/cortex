import { useState } from "react";
import {
  Alert,
  Badge,
  Button,
  Checkbox,
  Group,
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
import type { Category, CustomFieldDataType, CustomFieldDefCreatePayload } from "../../api/types";

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
  /** Called after a field is successfully added so the parent can refetch
   * the category list (field_defs are embedded on `Category`, per
   * `CategorySerializer`, so there is no separate list to invalidate). */
  onFieldAdded: () => void;
}

/**
 * Manages the `CustomFieldDef` list for one category (T1.5).
 *
 * NOTE (flagged for backend-engineer, see `api/types.ts`
 * `CustomFieldDefCreatePayload` doc comment): `docs/api-and-ui.md` only
 * documents `GET`/`POST /categories/{id}/fields` — there is no endpoint to
 * edit, reorder, or delete an existing field def. This panel only builds
 * against what's documented: it lists existing defs (read-only, including
 * `order`) and lets an admin add new ones (with an `order` at creation time,
 * so at least the initial ordering is controllable). Edit/delete/reorder
 * controls are intentionally not built until that endpoint exists — building
 * them against a shape the server doesn't support would just 404/500.
 */
export function CategoryFieldsPanel({ category, canManage, onFieldAdded }: CategoryFieldsPanelProps) {
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

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

  return (
    <Paper withBorder p="md" radius="md">
      <Stack gap="sm">
        <Title order={4}>Custom fields — {category.name}</Title>

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
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {sortedFieldDefs.map((fd) => (
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
          <>
            <Tooltip
              label="The API only supports adding new field defs — editing, reordering, or deleting an existing one isn't exposed yet (flagged for backend-engineer)."
              multiline
              w={260}
            >
              <Text size="xs" c="dimmed" style={{ cursor: "help" }}>
                Existing fields are read-only for now (hover for why).
              </Text>
            </Tooltip>

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
                {form.values.data_type === "enum" && (
                  <TagsInputForEnum form={form} />
                )}
                <Checkbox label="Required" {...form.getInputProps("required", { type: "checkbox" })} />
                <Button type="submit" loading={submitting}>
                  Add field
                </Button>
              </Stack>
            </form>
          </>
        )}
      </Stack>
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
