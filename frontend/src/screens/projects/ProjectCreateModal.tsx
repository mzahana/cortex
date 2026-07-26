import { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Group,
  Modal,
  NumberInput,
  Select,
  Stack,
  TextInput,
  Textarea,
} from "@mantine/core";
import { useForm } from "@mantine/form";
import { api, ApiError } from "../../api/client";
import type { AppUser, ProjectFundingSource, ProjectStatus } from "../../api/types";

interface ProjectCreateFormValues {
  name: string;
  code: string;
  lead_user: string | null;
  funding_source: ProjectFundingSource | "";
  sponsor: string;
  budget_total: number | "";
  currency: string;
  status: ProjectStatus;
  description: string;
}

const EMPTY_VALUES: ProjectCreateFormValues = {
  name: "",
  code: "",
  lead_user: null,
  funding_source: "",
  sponsor: "",
  budget_total: "",
  currency: "",
  status: "active",
  description: "",
};

interface ProjectCreateModalProps {
  opened: boolean;
  onClose: () => void;
  onCreated: () => void;
}

/**
 * Admin-only (`tenant.manage`) "New project" modal for the M7 Projects hub
 * list (`docs/tasks/M7-project-grants.md`: "Create/edit project... create/
 * delete are Admin-only"). Unlike the pre-M7 `admin/ProjectFormModal` (name/
 * lead/is_active only), this collects the grant metadata up front since
 * `POST /api/v1/projects` accepts the full M7 field set directly
 * (`ProjectCreatePayload` doc comment) — a lead can always come back and
 * fill in/adjust the rest from the hub's Overview tab later
 * (`project.manage`-gated `PATCH`).
 */
export function ProjectCreateModal({ opened, onClose, onCreated }: ProjectCreateModalProps) {
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [users, setUsers] = useState<AppUser[]>([]);

  const form = useForm<ProjectCreateFormValues>({
    initialValues: EMPTY_VALUES,
    validate: {
      name: (v) => (v.trim() ? null : "Name is required"),
      currency: (v) => (v && v.length > 3 ? "Use a 3-letter currency code (e.g. USD)." : null),
    },
  });

  useEffect(() => {
    if (!opened) return;
    setFormError(null);
    form.setValues(EMPTY_VALUES);
    api
      .listUsers({ page_size: 100 })
      .then((body) => setUsers(body.results))
      .catch(() => setUsers([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [opened]);

  const userOptions = users.map((u) => ({ value: String(u.id), label: u.name || u.email }));

  const handleSubmit = async (values: ProjectCreateFormValues) => {
    setFormError(null);
    setSubmitting(true);
    try {
      await api.createProject({
        name: values.name.trim(),
        code: values.code.trim() || undefined,
        lead_user: values.lead_user ? Number(values.lead_user) : null,
        funding_source: values.funding_source || undefined,
        sponsor: values.sponsor.trim() || undefined,
        budget_total: values.budget_total === "" ? undefined : String(values.budget_total),
        currency: values.currency.trim() || undefined,
        status: values.status,
        description: values.description.trim() || undefined,
      });
      onCreated();
      onClose();
    } catch (err) {
      if (err instanceof ApiError && err.problem.errors) {
        const { non_field_errors: nonField, ...fieldErrors } = err.problem.errors;
        if (Object.keys(fieldErrors).length > 0) {
          form.setErrors(
            Object.fromEntries(
              Object.entries(fieldErrors).map(([k, v]) => [
                k,
                Array.isArray(v) ? v.join(" ") : String(v),
              ]),
            ),
          );
        }
        if (nonField) {
          setFormError(Array.isArray(nonField) ? nonField.join(" ") : String(nonField));
        }
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
    <Modal opened={opened} onClose={onClose} title="New project" centered size="lg">
      <form onSubmit={form.onSubmit(handleSubmit)} noValidate>
        <Stack gap="sm">
          {formError && (
            <Alert color="red" data-testid="project-create-form-error">
              {formError}
            </Alert>
          )}
          <TextInput label="Name" required autoFocus {...form.getInputProps("name")} />
          <Group grow>
            <TextInput label="Code" placeholder="e.g. NSF-2026-014" {...form.getInputProps("code")} />
            <Select
              label="Project lead"
              placeholder="(none)"
              data={userOptions}
              clearable
              searchable
              {...form.getInputProps("lead_user")}
            />
          </Group>
          <Group grow>
            <Select
              label="Funding source"
              placeholder="(unset)"
              data={[
                { value: "internal", label: "Internal" },
                { value: "external", label: "External" },
              ]}
              clearable
              {...form.getInputProps("funding_source")}
            />
            <TextInput label="Sponsor" {...form.getInputProps("sponsor")} />
          </Group>
          <Group grow>
            <NumberInput
              label="Budget total"
              decimalScale={2}
              min={0}
              {...form.getInputProps("budget_total")}
            />
            <TextInput label="Currency" placeholder="USD" {...form.getInputProps("currency")} />
          </Group>
          <Select
            label="Status"
            data={[
              { value: "active", label: "Active" },
              { value: "closed", label: "Closed" },
            ]}
            allowDeselect={false}
            {...form.getInputProps("status")}
          />
          <Textarea label="Description" autosize minRows={2} {...form.getInputProps("description")} />
          <Button type="submit" loading={submitting} fullWidth mt="sm" data-testid="project-create-submit">
            Create project
          </Button>
        </Stack>
      </form>
    </Modal>
  );
}
