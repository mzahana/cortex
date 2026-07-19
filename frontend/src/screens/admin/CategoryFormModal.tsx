import { useEffect, useState } from "react";
import { Alert, Button, Checkbox, Modal, Select, Stack, TextInput } from "@mantine/core";
import { useForm } from "@mantine/form";
import { api, ApiError } from "../../api/client";
import type { Category } from "../../api/types";
import { collectDescendantIds, flattenForSelect, type TreeNode } from "../../components/treeUtils";

interface CategoryFormValues {
  name: string;
  parent: string | null;
  default_is_consumable: boolean;
  requires_approval: boolean;
  requires_calibration: boolean;
}

interface CategoryFormModalProps {
  opened: boolean;
  onClose: () => void;
  onSaved: () => void;
  /** The full tree, for the parent picker. */
  tree: TreeNode<Category>[];
  /** Editing an existing category, or `null` for create. */
  editing: Category | null;
  /** Preset parent id when creating a child from a tree row's "+" action. */
  presetParentId: number | null;
}

/** Create/edit modal for a `Category` node (T1.5). Shared by "Add root",
 * "Add child", and "Edit" — the only difference is `editing`/`presetParentId`. */
export function CategoryFormModal({
  opened,
  onClose,
  onSaved,
  tree,
  editing,
  presetParentId,
}: CategoryFormModalProps) {
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const form = useForm<CategoryFormValues>({
    initialValues: {
      name: "",
      parent: null,
      default_is_consumable: false,
      requires_approval: false,
      requires_calibration: false,
    },
    validate: {
      name: (value) => (value.trim() ? null : "Name is required"),
    },
  });

  useEffect(() => {
    if (!opened) return;
    setFormError(null);
    if (editing) {
      form.setValues({
        name: editing.name,
        parent: editing.parent !== null ? String(editing.parent) : null,
        default_is_consumable: editing.default_is_consumable,
        requires_approval: editing.requires_approval,
        requires_calibration: editing.requires_calibration,
      });
    } else {
      form.setValues({
        name: "",
        parent: presetParentId !== null ? String(presetParentId) : null,
        default_is_consumable: false,
        requires_approval: false,
        requires_calibration: false,
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [opened, editing, presetParentId]);

  // A node (and its own subtree) can never legally become its own parent —
  // excluded here so the picker doesn't offer an obviously-cyclic choice;
  // the server (`_validate_no_tree_cycle`) is the real enforcement.
  const excludeIds = new Set<number>();
  if (editing) {
    excludeIds.add(editing.id);
    const findNode = (nodes: TreeNode<Category>[]): TreeNode<Category> | null => {
      for (const n of nodes) {
        if (n.id === editing.id) return n;
        const found = findNode(n.children);
        if (found) return found;
      }
      return null;
    };
    const editingNode = findNode(tree);
    if (editingNode) {
      for (const id of collectDescendantIds(editingNode)) excludeIds.add(id);
    }
  }
  const parentOptions = flattenForSelect(tree, excludeIds);

  const handleSubmit = async (values: CategoryFormValues) => {
    setFormError(null);
    setSubmitting(true);
    try {
      const payload = {
        name: values.name.trim(),
        parent: values.parent ? Number(values.parent) : null,
        default_is_consumable: values.default_is_consumable,
        requires_approval: values.requires_approval,
        requires_calibration: values.requires_calibration,
      };
      if (editing) {
        await api.updateCategory(editing.id, payload);
      } else {
        await api.createCategory(payload);
      }
      onSaved();
      onClose();
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
    <Modal opened={opened} onClose={onClose} title={editing ? "Edit category" : "Add category"} centered>
      <form onSubmit={form.onSubmit(handleSubmit)} noValidate>
        <Stack gap="sm">
          {formError && (
            <Alert color="red" data-testid="category-form-error">
              {formError}
            </Alert>
          )}
          <TextInput label="Name" required autoFocus {...form.getInputProps("name")} />
          <Select
            label="Parent"
            placeholder="(root category)"
            data={parentOptions}
            clearable
            searchable
            {...form.getInputProps("parent")}
          />
          <Checkbox
            label="Consumable by default"
            description="New assets under this category default to is_consumable = true"
            {...form.getInputProps("default_is_consumable", { type: "checkbox" })}
          />
          <Checkbox
            label="Requires approval"
            description="Reservations/checkouts of assets in this category need approval"
            {...form.getInputProps("requires_approval", { type: "checkbox" })}
          />
          <Checkbox
            label="Requires calibration"
            {...form.getInputProps("requires_calibration", { type: "checkbox" })}
          />
          <Button type="submit" loading={submitting} fullWidth mt="sm">
            {editing ? "Save changes" : "Create category"}
          </Button>
        </Stack>
      </form>
    </Modal>
  );
}
