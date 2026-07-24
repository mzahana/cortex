import { useEffect, useState } from "react";
import { Alert, Button, Modal, Select, Stack, Switch, TextInput } from "@mantine/core";
import { useForm } from "@mantine/form";
import { api, ApiError } from "../../api/client";
import type { AppUser, Project } from "../../api/types";

interface ProjectFormValues {
  name: string;
  lead_user: string | null;
  is_active: boolean;
}

interface ProjectFormModalProps {
  opened: boolean;
  onClose: () => void;
  onSaved: () => void;
  editing: Project | null;
}

/** Create/edit modal for a `Project` (the "Admin: Users & Roles" screen's
 * project-scope picker needs at least one Project to exist before a
 * Member/Project-Lead membership can be scoped to it — this is the screen
 * that creates one). `lead_user` is fetched as a single bounded page (up to
 * `page_size=100`) rather than a live search-as-you-type picker like
 * `AddMemberModal`'s user search — a lab tenant's user list is small "bounded
 * config", same reasoning `apps.catalog.api.ProjectViewSet`'s docstring
 * already gives for treating Projects themselves this way. */
export function ProjectFormModal({ opened, onClose, onSaved, editing }: ProjectFormModalProps) {
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [users, setUsers] = useState<AppUser[]>([]);

  const form = useForm<ProjectFormValues>({
    initialValues: { name: "", lead_user: null, is_active: true },
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
        lead_user: editing.lead_user !== null ? String(editing.lead_user) : null,
        is_active: editing.is_active,
      });
    } else {
      form.setValues({ name: "", lead_user: null, is_active: true });
    }
    api
      .listUsers({ page_size: 100 })
      .then((body) => setUsers(body.results))
      .catch(() => setUsers([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [opened, editing]);

  const userOptions = users.map((u) => ({ value: String(u.id), label: u.name || u.email }));

  const handleSubmit = async (values: ProjectFormValues) => {
    setFormError(null);
    setSubmitting(true);
    try {
      const payload = {
        name: values.name.trim(),
        lead_user: values.lead_user ? Number(values.lead_user) : null,
        is_active: values.is_active,
      };
      if (editing) {
        await api.updateProject(editing.id, payload);
      } else {
        await api.createProject(payload);
      }
      onSaved();
      onClose();
    } catch (err) {
      if (err instanceof ApiError && err.problem.errors) {
        form.setErrors(
          Object.fromEntries(
            Object.entries(err.problem.errors).map(([k, v]) => [
              k,
              Array.isArray(v) ? v.join(" ") : String(v),
            ]),
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
    <Modal opened={opened} onClose={onClose} title={editing ? "Edit project" : "New project"} centered>
      <form onSubmit={form.onSubmit(handleSubmit)} noValidate>
        <Stack gap="sm">
          {formError && (
            <Alert color="red" data-testid="project-form-error">
              {formError}
            </Alert>
          )}
          <TextInput label="Name" required autoFocus {...form.getInputProps("name")} />
          <Select
            label="Project lead"
            placeholder="(none)"
            data={userOptions}
            clearable
            searchable
            {...form.getInputProps("lead_user")}
          />
          <Switch
            label="Active"
            checked={form.values.is_active}
            onChange={(e) => form.setFieldValue("is_active", e.currentTarget.checked)}
          />
          <Button type="submit" loading={submitting} fullWidth mt="sm" data-testid="project-form-submit">
            {editing ? "Save changes" : "Create project"}
          </Button>
        </Stack>
      </form>
    </Modal>
  );
}
