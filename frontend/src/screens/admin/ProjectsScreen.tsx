import { useCallback, useEffect, useState } from "react";
import {
  ActionIcon,
  Alert,
  Badge,
  Button,
  Center,
  Group,
  Loader,
  Table,
  Text,
  Tooltip,
} from "@mantine/core";
import { api, ApiError } from "../../api/client";
import { hasPermission, TENANT_MANAGE } from "../../api/permissions";
import { useAuth } from "../../hooks/useAuth";
import { AppLayout } from "../../layout/AppLayout";
import type { AppUser, Project } from "../../api/types";
import { ConfirmDeleteModal } from "../../components/ConfirmDeleteModal";
import { ProjectFormModal } from "./ProjectFormModal";

/**
 * Admin: Projects. Was entirely missing from the frontend despite
 * `apps.catalog.api.ProjectViewSet` being a full CRUD endpoint from M1 --
 * discovered as a real gap while wiring the Users & Roles screen's
 * project-scope picker (a Member/Project-Lead membership can only be
 * scoped to a Project that already exists, and there was no UI to create
 * one). Flat list, not a tree (`Project` has no parent/child concept,
 * unlike Category/Location) -- gated by `tenant.manage`, the closest
 * documented analog for Project writes (see `ProjectViewSet`'s own
 * docstring; rbac.md has no dedicated `project.manage` key).
 */
export function ProjectsScreen() {
  const { me } = useAuth();
  const canManage = hasPermission(me, TENANT_MANAGE);

  const [projects, setProjects] = useState<Project[] | null>(null);
  const [users, setUsers] = useState<Map<number, AppUser>>(new Map());
  const [loadError, setLoadError] = useState<string | null>(null);

  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<Project | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Project | null>(null);

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      const [all, userPage] = await Promise.all([
        api.listAllProjects({ ordering: "name" }),
        // Best-effort name resolution for `lead_user` -- a 403 here (a
        // `tenant.manage`-only role without `user.manage`) just falls back
        // to showing "User #id" below, same graceful-degradation pattern
        // `AssetDetailScreen` used before T5.3 added a lookup endpoint.
        api.listUsers({ page_size: 100 }).catch(() => ({ results: [] as AppUser[] })),
      ]);
      setProjects(all);
      setUsers(new Map(userPage.results.map((u) => [u.id, u])));
    } catch (err) {
      setLoadError(
        err instanceof ApiError
          ? err.problem.detail ?? err.problem.title
          : "Unable to reach the server. Please try again.",
      );
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const openCreate = () => {
    setEditing(null);
    setFormOpen(true);
  };

  const openEdit = (project: Project) => {
    setEditing(project);
    setFormOpen(true);
  };

  return (
    <AppLayout
      title="Projects"
      actions={
        canManage ? (
          <Button size="xs" onClick={openCreate} data-testid="new-project-button">
            New project
          </Button>
        ) : (
          <Badge variant="light" color="gray">
            Read-only
          </Badge>
        )
      }
    >
      {projects === null && !loadError && (
        <Center p="xl">
          <Loader />
        </Center>
      )}

      {loadError && (
        <Alert color="red" mb="md" data-testid="projects-load-error">
          {loadError}
        </Alert>
      )}

      {projects !== null && projects.length === 0 && !loadError && (
        <Text c="dimmed" size="sm">
          No projects yet. Projects let you scope a Member or Project Lead's role to a
          specific area instead of the whole tenant — create one to get started.
        </Text>
      )}

      {projects !== null && projects.length > 0 && (
        <Table verticalSpacing="sm" data-testid="projects-table">
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Name</Table.Th>
              <Table.Th>Lead</Table.Th>
              <Table.Th>Status</Table.Th>
              {canManage && <Table.Th />}
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {projects.map((project) => (
              <Table.Tr key={project.id} data-testid={`project-row-${project.id}`}>
                <Table.Td>{project.name}</Table.Td>
                <Table.Td>
                  {project.lead_user !== null ? (
                    users.get(project.lead_user)?.name ??
                    users.get(project.lead_user)?.email ??
                    `User #${project.lead_user}`
                  ) : (
                    <Text c="dimmed" size="sm">
                      —
                    </Text>
                  )}
                </Table.Td>
                <Table.Td>
                  <Badge color={project.is_active ? "teal" : "gray"} variant="light">
                    {project.is_active ? "Active" : "Inactive"}
                  </Badge>
                </Table.Td>
                {canManage && (
                  <Table.Td>
                    <Group gap={4} justify="flex-end" wrap="nowrap">
                      <Tooltip label="Edit">
                        <ActionIcon
                          variant="subtle"
                          size="sm"
                          aria-label={`Edit ${project.name}`}
                          onClick={() => openEdit(project)}
                        >
                          ✎
                        </ActionIcon>
                      </Tooltip>
                      <Tooltip label="Delete">
                        <ActionIcon
                          variant="subtle"
                          size="sm"
                          color="red"
                          aria-label={`Delete ${project.name}`}
                          onClick={() => setDeleteTarget(project)}
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

      <ProjectFormModal
        opened={formOpen}
        onClose={() => setFormOpen(false)}
        onSaved={() => void load()}
        editing={editing}
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
          onDeleted={() => void load()}
        />
      )}
    </AppLayout>
  );
}
