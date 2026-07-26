import { useEffect, useState } from "react";
import {
  Alert,
  Badge,
  Button,
  Card,
  Group,
  NumberInput,
  Progress,
  Select,
  SimpleGrid,
  Stack,
  Table,
  Text,
  TextInput,
  Textarea,
  Title,
} from "@mantine/core";
import { DateInput } from "@mantine/dates";
import { useForm } from "@mantine/form";
import { IconLock } from "@tabler/icons-react";
import { api, ApiError } from "../../api/client";
import type { AppUser, ProjectDetail, ProjectFundingSource, ProjectStatus } from "../../api/types";

interface GrantFormValues {
  name: string;
  code: string;
  lead_user: string | null;
  funding_source: ProjectFundingSource | "";
  sponsor: string;
  start_date: Date | null;
  end_date: Date | null;
  budget_total: number | "";
  currency: string;
  status: ProjectStatus;
  description: string;
}

function toFormValues(project: ProjectDetail): GrantFormValues {
  return {
    name: project.name,
    code: project.code,
    lead_user: project.lead_user !== null ? String(project.lead_user) : null,
    funding_source: project.funding_source,
    sponsor: project.sponsor,
    start_date: project.start_date ? new Date(project.start_date) : null,
    end_date: project.end_date ? new Date(project.end_date) : null,
    budget_total: project.budget_total !== null ? Number(project.budget_total) : "",
    currency: project.currency,
    status: project.status,
    description: project.description,
  };
}

function toIsoDate(d: Date | null): string | null {
  if (!d) return null;
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

interface OverviewBudgetTabProps {
  project: ProjectDetail;
  canManage: boolean;
  onUpdated: (updated: ProjectDetail) => void;
}

/**
 * Project hub — Overview/Budget tab (`docs/tasks/M7-project-grants.md`):
 * grant metadata (editable, `project.manage`-gated) + `budget_total`/
 * `spent`/`remaining`/`spend_by_category` (read-only, server-computed).
 *
 * **Financial redaction vs. "budget not set yet" — do not conflate the
 * two.** `budget_total` alone is `null` in BOTH cases: (a) the caller is
 * redacted (no project-scoped `expense.view`), AND (b) an authorized viewer
 * (Admin/this project's Lead) looking at a project whose budget simply
 * hasn't been set (every project starts with `budget_total = null`). The
 * unambiguous redaction signal is `spent`/`remaining`/`spend_by_category`
 * (`apps.projects.serializers.ProjectDetailSerializer`/`budget_rollup`):
 * these three are `null` ONLY under redaction — an authorized caller always
 * gets at least `"0.00"` for `spent`, regardless of whether `budget_total`
 * is set. So `canViewFinancials` below keys off `project.spent`, never
 * `project.budget_total` — an authorized caller with no budget configured
 * yet sees spent/remaining/the breakdown with "Budget total: not set",
 * never the lock panel; only a genuinely redacted caller sees that.
 *
 * The spend-by-category breakdown is rendered as a plain Mantine `Progress`
 * bar + table (proportion of total spend per category) rather than a real
 * chart library — no `recharts`/`nivo`/etc. dependency added, so the
 * `dataviz` skill doesn't apply here (there's no such skill in this repo;
 * checked `.claude/skills` before building this).
 */
export function OverviewBudgetTab({ project, canManage, onUpdated }: OverviewBudgetTabProps) {
  const [users, setUsers] = useState<AppUser[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const form = useForm<GrantFormValues>({ initialValues: toFormValues(project) });

  useEffect(() => {
    form.setValues(toFormValues(project));
    setSaved(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project]);

  useEffect(() => {
    api
      .listUsers({ page_size: 100 })
      .then((body) => setUsers(body.results))
      .catch(() => setUsers([]));
  }, []);

  const userOptions = users.map((u) => ({ value: String(u.id), label: u.name || u.email }));
  // `spent` (not `budget_total`) is the unambiguous redaction sentinel — see
  // this component's own doc comment above for why.
  const canViewFinancials = project.spent !== null;

  const handleSubmit = async (values: GrantFormValues) => {
    setFormError(null);
    setSaved(false);
    setSubmitting(true);
    try {
      const updated = await api.updateProjectDetail(project.id, {
        name: values.name.trim(),
        code: values.code.trim(),
        lead_user: values.lead_user ? Number(values.lead_user) : null,
        funding_source: values.funding_source || "",
        sponsor: values.sponsor.trim(),
        start_date: toIsoDate(values.start_date),
        end_date: toIsoDate(values.end_date),
        budget_total: values.budget_total === "" ? null : String(values.budget_total),
        currency: values.currency.trim(),
        status: values.status,
        description: values.description.trim(),
      });
      onUpdated(updated);
      setSaved(true);
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
        if (nonField) setFormError(Array.isArray(nonField) ? nonField.join(" ") : String(nonField));
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
    <Stack gap="lg" data-testid="overview-budget-tab">
      <Card withBorder padding="md">
        <Title order={5} mb="sm">
          Budget
        </Title>
        {!canViewFinancials ? (
          <Alert color="gray" icon={<IconLock size={16} />} data-testid="financials-locked">
            You don&apos;t have access to this project&apos;s financials. Ask an Admin or this
            project&apos;s Lead to grant you project-scoped access.
          </Alert>
        ) : (
          <Stack gap="md">
            <SimpleGrid cols={{ base: 1, sm: 3 }}>
              <Stat
                label="Budget total"
                value={project.budget_total}
                currency={project.currency}
                unsetLabel="Not set"
              />
              <Stat label="Spent" value={project.spent} currency={project.currency} />
              <Stat label="Remaining" value={project.remaining} currency={project.currency} />
            </SimpleGrid>

            {project.spend_by_category && project.spend_by_category.length > 0 ? (
              <Table verticalSpacing="xs" data-testid="spend-by-category-table">
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>Category</Table.Th>
                    <Table.Th>Spent</Table.Th>
                    <Table.Th style={{ width: "40%" }}>Share of total spend</Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {project.spend_by_category.map((row) => {
                    const spentTotal = Number(project.spent ?? 0);
                    const pct = spentTotal > 0 ? (Number(row.total) / spentTotal) * 100 : 0;
                    return (
                      <Table.Tr key={row.category_id ?? "uncategorized"}>
                        <Table.Td>{row.category ?? "Uncategorized"}</Table.Td>
                        <Table.Td>
                          {project.currency ? `${project.currency} ` : ""}
                          {row.total}
                        </Table.Td>
                        <Table.Td>
                          <Progress value={pct} size="lg" />
                        </Table.Td>
                      </Table.Tr>
                    );
                  })}
                </Table.Tbody>
              </Table>
            ) : (
              <Text c="dimmed" size="sm">
                No expenses recorded yet.
              </Text>
            )}
          </Stack>
        )}
      </Card>

      <Card withBorder padding="md">
        <Group justify="space-between" mb="sm">
          <Title order={5}>Grant details</Title>
          {!canManage && (
            <Badge variant="light" color="gray">
              Read-only
            </Badge>
          )}
        </Group>

        {formError && (
          <Alert color="red" mb="sm" data-testid="overview-form-error">
            {formError}
          </Alert>
        )}
        {saved && (
          <Alert color="teal" mb="sm" data-testid="overview-form-saved">
            Saved.
          </Alert>
        )}

        <form onSubmit={form.onSubmit(handleSubmit)} noValidate>
          <Stack gap="sm">
            <Group grow>
              <TextInput label="Name" disabled={!canManage} required {...form.getInputProps("name")} />
              <TextInput label="Code" disabled={!canManage} {...form.getInputProps("code")} />
            </Group>
            <Group grow>
              <Select
                label="Project lead"
                placeholder="(none)"
                data={userOptions}
                clearable
                searchable
                disabled={!canManage}
                {...form.getInputProps("lead_user")}
              />
              <Select
                label="Status"
                data={[
                  { value: "active", label: "Active" },
                  { value: "closed", label: "Closed" },
                ]}
                allowDeselect={false}
                disabled={!canManage}
                {...form.getInputProps("status")}
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
                disabled={!canManage}
                {...form.getInputProps("funding_source")}
              />
              <TextInput label="Sponsor" disabled={!canManage} {...form.getInputProps("sponsor")} />
            </Group>
            <Group grow>
              <DateInput
                label="Start date"
                clearable
                disabled={!canManage}
                {...form.getInputProps("start_date")}
              />
              <DateInput
                label="End date"
                clearable
                disabled={!canManage}
                {...form.getInputProps("end_date")}
              />
            </Group>
            <Group grow>
              <NumberInput
                label="Budget total"
                decimalScale={2}
                min={0}
                disabled={!canManage}
                {...form.getInputProps("budget_total")}
              />
              <TextInput label="Currency" disabled={!canManage} {...form.getInputProps("currency")} />
            </Group>
            <Textarea
              label="Description"
              autosize
              minRows={2}
              disabled={!canManage}
              {...form.getInputProps("description")}
            />
            {canManage && (
              <Button type="submit" loading={submitting} data-testid="overview-save-button">
                Save changes
              </Button>
            )}
          </Stack>
        </form>
      </Card>
    </Stack>
  );
}

function Stat({
  label,
  value,
  currency,
  unsetLabel = "—",
}: {
  label: string;
  value: string | null;
  currency: string;
  /** Rendered when `value` is `null` — this component is only ever reached
   * once `canViewFinancials` is already true (an authorized caller), so a
   * `null` here always means "not set" (e.g. `budget_total` on a fresh
   * project), never "redacted" — the caller picks the wording (`"Not set"`
   * for budget, the default em dash for `spent`/`remaining`, which are
   * never actually `null` for an authorized caller — `budget_rollup`
   * always returns at least `"0.00"`). */
  unsetLabel?: string;
}) {
  return (
    <Card withBorder padding="sm">
      <Text size="xs" c="dimmed">
        {label}
      </Text>
      <Text size="lg" fw={700}>
        {value === null ? unsetLabel : `${currency ? `${currency} ` : ""}${value}`}
      </Text>
    </Card>
  );
}
