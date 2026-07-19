import { useEffect, useState } from "react";
import { Alert, Button, Modal, Select, Stack, TextInput } from "@mantine/core";
import { useForm } from "@mantine/form";
import { api, ApiError } from "../../api/client";
import type { Location } from "../../api/types";
import { collectDescendantIds, flattenForSelect, type TreeNode } from "../../components/treeUtils";

interface LocationFormValues {
  name: string;
  parent: string | null;
  kind: string;
}

interface LocationFormModalProps {
  opened: boolean;
  onClose: () => void;
  onSaved: () => void;
  tree: TreeNode<Location>[];
  editing: Location | null;
  presetParentId: number | null;
}

/** Create/edit modal for a `Location` node (T1.5) — mirrors
 * `CategoryFormModal` but with `Location`'s smaller field set (`kind` is a
 * free string per `apps.catalog.models.Location`, no enforced vocabulary). */
export function LocationFormModal({
  opened,
  onClose,
  onSaved,
  tree,
  editing,
  presetParentId,
}: LocationFormModalProps) {
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const form = useForm<LocationFormValues>({
    initialValues: { name: "", parent: null, kind: "" },
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
        kind: editing.kind,
      });
    } else {
      form.setValues({
        name: "",
        parent: presetParentId !== null ? String(presetParentId) : null,
        kind: "",
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [opened, editing, presetParentId]);

  const excludeIds = new Set<number>();
  if (editing) {
    excludeIds.add(editing.id);
    const findNode = (nodes: TreeNode<Location>[]): TreeNode<Location> | null => {
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

  const handleSubmit = async (values: LocationFormValues) => {
    setFormError(null);
    setSubmitting(true);
    try {
      const payload = {
        name: values.name.trim(),
        parent: values.parent ? Number(values.parent) : null,
        kind: values.kind.trim(),
      };
      if (editing) {
        await api.updateLocation(editing.id, payload);
      } else {
        await api.createLocation(payload);
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
    <Modal opened={opened} onClose={onClose} title={editing ? "Edit location" : "Add location"} centered>
      <form onSubmit={form.onSubmit(handleSubmit)} noValidate>
        <Stack gap="sm">
          {formError && (
            <Alert color="red" data-testid="location-form-error">
              {formError}
            </Alert>
          )}
          <TextInput label="Name" required autoFocus {...form.getInputProps("name")} />
          <Select
            label="Parent"
            placeholder="(root location)"
            data={parentOptions}
            clearable
            searchable
            {...form.getInputProps("parent")}
          />
          <TextInput
            label="Kind"
            placeholder="Room, Shelf, Cabinet, Bin, ..."
            description="Free-form — no enforced vocabulary"
            {...form.getInputProps("kind")}
          />
          <Button type="submit" loading={submitting} fullWidth mt="sm">
            {editing ? "Save changes" : "Create location"}
          </Button>
        </Stack>
      </form>
    </Modal>
  );
}
