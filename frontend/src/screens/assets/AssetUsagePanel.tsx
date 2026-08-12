/**
 * "Used by" panel on the asset detail screen (M8 Phase 1,
 * `docs/tasks/M8-expense-reconciliation.md` §1.6, §5).
 *
 * An asset has ONE funding project (`Asset.project` — who paid, shown as a
 * plain read-only field elsewhere on the detail screen) and MANY using
 * projects, listed here. The distinction is the whole point of the panel, and
 * the copy says so out loud: **adding a row here moves no cost between
 * projects.** Users reasonably assume that linking a project to equipment
 * charges that project for it; if the UI let them believe that, the resulting
 * "why is this drone on two grants" question is exactly the audit problem M8
 * exists to remove.
 *
 * Writes are gated on `asset.edit` scoped to the asset's OWN funding project
 * (`canEdit`, passed down from the detail screen, which already computes it) —
 * matching the server rule in `apps.assets.permissions._usages_permission_key`.
 * UI gating is never the boundary; the server re-checks.
 */
import { useCallback, useEffect, useState } from "react";
import {
  ActionIcon,
  Alert,
  Badge,
  Button,
  Card,
  Group,
  Select,
  Stack,
  Text,
  TextInput,
  Title,
} from "@mantine/core";
import { DateInput } from "@mantine/dates";

import { api } from "../../api/client";
import { ApiError } from "../../api/problem";
import type { AssetProjectUsage, Project } from "../../api/types";

interface AssetUsagePanelProps {
  assetId: number;
  /** The asset's FUNDING project id — excluded from the picker below, since
   * "the project that paid for it uses it" is the default assumption and
   * recording it as usage would just be noise. */
  fundingProjectId: number | null;
  /** Tenant projects available to pick from (already loaded by the parent). */
  projects: Project[];
  /** `asset.edit` on the asset's funding project — see module docstring. */
  canEdit: boolean;
}

function toIsoDate(d: Date | null): string | null {
  if (!d) return null;
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(
    d.getDate(),
  ).padStart(2, "0")}`;
}

export function AssetUsagePanel({
  assetId,
  fundingProjectId,
  projects,
  canEdit,
}: AssetUsagePanelProps) {
  const [usages, setUsages] = useState<AssetProjectUsage[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);

  const [adding, setAdding] = useState(false);
  const [newProject, setNewProject] = useState<string | null>(null);
  const [newStart, setNewStart] = useState<Date | null>(new Date());
  const [newNote, setNewNote] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const page = await api.listAssetUsages(assetId);
      setUsages(page.results);
    } catch {
      setError("Could not load usage records.");
    } finally {
      setLoading(false);
    }
  }, [assetId]);

  useEffect(() => {
    void load();
  }, [load]);

  // Already-open usages can't be duplicated (the server's partial unique
  // index returns 409), so filter those projects out rather than letting the
  // user pick one and hit an avoidable error.
  const openProjectIds = new Set(
    usages.filter((u) => !u.end_date).map((u) => u.project),
  );
  const options = projects
    .filter((p) => p.id !== fundingProjectId && !openProjectIds.has(p.id))
    .map((p) => ({ value: String(p.id), label: p.name }));

  async function handleAdd() {
    if (!newProject) return;
    setSubmitting(true);
    setError(null);
    try {
      await api.createAssetUsage(assetId, {
        project: Number(newProject),
        start_date: toIsoDate(newStart),
        note: newNote.trim(),
      });
      setAdding(false);
      setNewProject(null);
      setNewStart(new Date());
      setNewNote("");
      await load();
    } catch (err) {
      setError(
        err instanceof ApiError && err.status === 409
          ? "That project is already recorded as currently using this asset."
          : "Could not add the usage record.",
      );
    } finally {
      setSubmitting(false);
    }
  }

  async function handleEnd(usage: AssetProjectUsage) {
    setBusyId(usage.id);
    setError(null);
    try {
      await api.updateAssetUsage(usage.id, { end_date: toIsoDate(new Date()) });
      await load();
    } catch {
      setError("Could not end the usage record.");
    } finally {
      setBusyId(null);
    }
  }

  async function handleDelete(usage: AssetProjectUsage) {
    setBusyId(usage.id);
    setError(null);
    try {
      await api.deleteAssetUsage(usage.id);
      await load();
    } catch {
      setError("Could not remove the usage record.");
    } finally {
      setBusyId(null);
    }
  }

  return (
    <Card withBorder data-testid="asset-usage-panel">
      <Stack gap="sm">
        <Group justify="space-between" align="center">
          <Title order={5}>Used by</Title>
          {canEdit && !adding && (
            <Button size="xs" variant="light" onClick={() => setAdding(true)}>
              Add project
            </Button>
          )}
        </Group>

        <Text size="xs" c="dimmed">
          Projects using this asset. Its cost stays with the project that funded
          it — adding a project here moves no money.
        </Text>

        {error && (
          <Alert color="red" variant="light">
            {error}
          </Alert>
        )}

        {loading ? (
          <Text size="sm" c="dimmed">
            Loading…
          </Text>
        ) : usages.length === 0 ? (
          <Text size="sm" c="dimmed">
            No other projects are recorded as using this asset.
          </Text>
        ) : (
          <Stack gap="xs">
            {usages.map((usage) => (
              <Group key={usage.id} justify="space-between" wrap="nowrap">
                <Stack gap={0}>
                  <Group gap="xs">
                    <Text size="sm">{usage.project_name}</Text>
                    {!usage.end_date && (
                      <Badge size="xs" color="green" variant="light">
                        Ongoing
                      </Badge>
                    )}
                  </Group>
                  <Text size="xs" c="dimmed">
                    {usage.start_date ?? "—"} → {usage.end_date ?? "present"}
                    {usage.note ? ` · ${usage.note}` : ""}
                  </Text>
                </Stack>
                {canEdit && (
                  <Group gap="xs" wrap="nowrap">
                    {!usage.end_date && (
                      <Button
                        size="compact-xs"
                        variant="subtle"
                        loading={busyId === usage.id}
                        onClick={() => void handleEnd(usage)}
                      >
                        End now
                      </Button>
                    )}
                    <ActionIcon
                      size="sm"
                      variant="subtle"
                      color="red"
                      aria-label={`Remove ${usage.project_name}`}
                      loading={busyId === usage.id}
                      onClick={() => void handleDelete(usage)}
                    >
                      ×
                    </ActionIcon>
                  </Group>
                )}
              </Group>
            ))}
          </Stack>
        )}

        {canEdit && adding && (
          <Stack gap="xs">
            <Select
              label="Project"
              placeholder="Pick a project"
              data={options}
              value={newProject}
              onChange={setNewProject}
              searchable
            />
            <DateInput
              label="Used from"
              value={newStart}
              onChange={setNewStart}
              clearable
            />
            <TextInput
              label="Note (optional)"
              value={newNote}
              onChange={(e) => setNewNote(e.currentTarget.value)}
            />
            <Group gap="xs">
              <Button
                size="xs"
                loading={submitting}
                onClick={() => void handleAdd()}
              >
                Save
              </Button>
              <Button
                size="xs"
                variant="subtle"
                onClick={() => setAdding(false)}
              >
                Cancel
              </Button>
            </Group>
          </Stack>
        )}
      </Stack>
    </Card>
  );
}
