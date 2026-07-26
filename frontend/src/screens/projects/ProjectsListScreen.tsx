import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ActionIcon,
  Alert,
  Badge,
  Button,
  Center,
  Group,
  Loader,
  Pagination,
  Select,
  Stack,
  Table,
  Text,
  TextInput,
  Tooltip,
} from "@mantine/core";
import { useDebouncedValue } from "@mantine/hooks";
import { IconLock } from "@tabler/icons-react";
import { useNavigate } from "react-router-dom";
import { api } from "../../api/client";
import { TENANT_MANAGE, hasPermission } from "../../api/permissions";
import { useAuth } from "../../hooks/useAuth";
import { AppLayout } from "../../layout/AppLayout";
import type { AppUser, ProjectListParams, ProjectStatus } from "../../api/types";
import { ConfirmDeleteModal } from "../../components/ConfirmDeleteModal";
import { ProjectCreateModal } from "./ProjectCreateModal";
import { useProjectList } from "./useProjectList";

const STATUS_OPTIONS: { value: ProjectStatus; label: string }[] = [
  { value: "active", label: "Active" },
  { value: "closed", label: "Closed" },
];

/**
 * Projects hub — top-level list screen (`/projects`, M7
 * `docs/tasks/M7-project-grants.md`), gated by `project.view` in `layout/
 * nav.ts`. Server-side paginated/searched/filtered (CLAUDE.md: never load
 * "all" of anything) — a pure ProjectLead sees only their own project(s)
 * (server-scoped, `apps.projects.api.ProjectViewSet.get_queryset`).
 *
 * **`budget_total` financial redaction**: the server nulls it out per-row
 * for any caller without project-scoped `expense.view` on THAT project
 * (`Project` type doc comment) — rendered here as a locked padlock
 * affordance, `"—"`, NEVER `"$0.00"` (a real awarded budget of exactly zero
 * is a valid, if unusual, string; `null` is the only "hidden" sentinel).
 *
 * Create/delete are Admin-only (`tenant.manage`) per the RBAC matrix; this
 * screen is the SOLE reachable "Projects" nav destination (the pre-M7 thin
 * `admin/ProjectsScreen` CRUD was dropped from nav, see `layout/nav.ts`'s
 * comment) — it now owns that same create/delete contract too.
 */
export function ProjectsListScreen() {
  const navigate = useNavigate();
  const { me } = useAuth();
  const canManage = hasPermission(me, TENANT_MANAGE);

  const [search, setSearch] = useState("");
  const [status, setStatus] = useState<ProjectStatus | null>(null);
  const [debouncedSearch] = useDebouncedValue(search, 350);

  const filters = useMemo<ProjectListParams>(
    () => ({
      search: debouncedSearch || undefined,
      status: status ?? undefined,
      ordering: "name",
    }),
    [debouncedSearch, status],
  );

  const { items, totalCount, page, pageCount, loading, error, setPage, reload } = useProjectList({
    filters,
  });

  // Best-effort lead-name resolution, same graceful-degradation pattern as
  // the pre-M7 admin ProjectsScreen: a `tenant.manage`-only Admin without
  // `user.manage` just sees "User #id" instead of a name.
  const [users, setUsers] = useState<Map<number, AppUser>>(new Map());
  useEffect(() => {
    api
      .listUsers({ page_size: 100 })
      .then((body) => setUsers(new Map(body.results.map((u) => [u.id, u]))))
      .catch(() => setUsers(new Map()));
  }, []);

  const [createOpen, setCreateOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<{ id: number; name: string } | null>(null);

  const leadName = useCallback(
    (leadUserId: number | null) => {
      if (leadUserId === null) return null;
      const user = users.get(leadUserId);
      return user?.name || user?.email || `User #${leadUserId}`;
    },
    [users],
  );

  return (
    <AppLayout
      title="Projects"
      actions={
        canManage ? (
          <Button size="xs" onClick={() => setCreateOpen(true)} data-testid="new-project-button">
            New project
          </Button>
        ) : (
          <Text size="sm" c="dimmed">
            {totalCount !== null ? `${items.length} of ${totalCount}` : ""}
          </Text>
        )
      }
    >
      <Stack gap="md">
        <Group grow wrap="wrap">
          <TextInput
            placeholder="Search name or code…"
            value={search}
            onChange={(e) => setSearch(e.currentTarget.value)}
            aria-label="Search projects"
            data-testid="project-search"
          />
          <Select
            placeholder="Status"
            data={STATUS_OPTIONS}
            value={status}
            onChange={(v) => setStatus(v as ProjectStatus | null)}
            clearable
            aria-label="Filter by status"
            data-testid="project-status-filter"
          />
        </Group>

        {error && (
          <Alert color="red" data-testid="project-list-error" title="Couldn't load projects">
            <Stack gap="xs" align="flex-start">
              <Text size="sm">{error}</Text>
              <Button size="xs" variant="light" onClick={reload}>
                Retry
              </Button>
            </Stack>
          </Alert>
        )}

        {loading && !error && (
          <Center p="xl">
            <Loader data-testid="project-list-loading" />
          </Center>
        )}

        {!loading && !error && items.length === 0 && (
          <Center p="xl">
            <Stack align="center" gap={4}>
              <Text fw={600}>No projects match these filters.</Text>
              <Text size="sm" c="dimmed">
                {canManage ? "Create one to get started." : "Try clearing a filter."}
              </Text>
            </Stack>
          </Center>
        )}

        {!loading && !error && items.length > 0 && (
          <Table.ScrollContainer minWidth={640}>
            <Table verticalSpacing="sm" data-testid="projects-table">
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>Name</Table.Th>
                  <Table.Th>Code</Table.Th>
                  <Table.Th>Funding</Table.Th>
                  <Table.Th>Status</Table.Th>
                  <Table.Th>Lead</Table.Th>
                  <Table.Th>Budget</Table.Th>
                  {canManage && <Table.Th />}
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {items.map((project) => (
                  <Table.Tr key={project.id} data-testid={`project-row-${project.id}`}>
                    <Table.Td>
                      <Text
                        component="button"
                        onClick={() => navigate(`/projects/${project.id}`)}
                        fw={600}
                        style={{
                          background: "none",
                          border: "none",
                          padding: 0,
                          cursor: "pointer",
                          textAlign: "left",
                          color: "var(--mantine-color-brand-6)",
                        }}
                      >
                        {project.name}
                      </Text>
                    </Table.Td>
                    <Table.Td>{project.code || <Text c="dimmed">—</Text>}</Table.Td>
                    <Table.Td>
                      {project.funding_source ? (
                        <Badge variant="light" color={project.funding_source === "external" ? "grape" : "blue"}>
                          {project.funding_source === "external" ? "External" : "Internal"}
                        </Badge>
                      ) : (
                        <Text c="dimmed">—</Text>
                      )}
                    </Table.Td>
                    <Table.Td>
                      <Badge color={project.status === "active" ? "teal" : "gray"} variant="light">
                        {project.status === "active" ? "Active" : "Closed"}
                      </Badge>
                    </Table.Td>
                    <Table.Td>
                      {leadName(project.lead_user) ?? <Text c="dimmed">—</Text>}
                    </Table.Td>
                    <Table.Td>
                      {project.budget_total === null ? (
                        <Tooltip label="You don't have access to this project's financials">
                          <Group gap={4} c="dimmed" data-testid={`project-budget-locked-${project.id}`}>
                            <IconLock size={14} />
                            <Text size="sm">—</Text>
                          </Group>
                        </Tooltip>
                      ) : (
                        <Text size="sm" data-testid={`project-budget-${project.id}`}>
                          {project.currency ? `${project.currency} ` : ""}
                          {project.budget_total}
                        </Text>
                      )}
                    </Table.Td>
                    {canManage && (
                      <Table.Td>
                        <Tooltip label="Delete">
                          <ActionIcon
                            variant="subtle"
                            size="sm"
                            color="red"
                            aria-label={`Delete ${project.name}`}
                            onClick={() => setDeleteTarget({ id: project.id, name: project.name })}
                          >
                            🗑
                          </ActionIcon>
                        </Tooltip>
                      </Table.Td>
                    )}
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </Table.ScrollContainer>
        )}

        {pageCount > 1 && (
          <Center>
            <Pagination total={pageCount} value={page} onChange={setPage} size="sm" />
          </Center>
        )}
      </Stack>

      <ProjectCreateModal
        opened={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={() => reload()}
      />

      {deleteTarget && (
        <ConfirmDeleteModal
          opened={!!deleteTarget}
          title="Delete project"
          itemLabel={deleteTarget.name}
          onClose={() => setDeleteTarget(null)}
          onConfirm={async () => {
            await api.deleteProject(deleteTarget.id);
          }}
          onDeleted={() => reload()}
        >
          <Alert color="yellow" mb="sm">
            This permanently deletes every expense, invoice scan, and document under this
            project too.
          </Alert>
        </ConfirmDeleteModal>
      )}
    </AppLayout>
  );
}
