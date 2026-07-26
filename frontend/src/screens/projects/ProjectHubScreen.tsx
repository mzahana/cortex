import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Center, Loader, Stack, Tabs, Text } from "@mantine/core";
import { useNavigate, useParams } from "react-router-dom";
import { api, ApiError } from "../../api/client";
import { PROJECT_MANAGE, hasProjectScopedPermission } from "../../api/permissions";
import { useAuth } from "../../hooks/useAuth";
import { AppLayout } from "../../layout/AppLayout";
import type { ProjectDetail } from "../../api/types";
import { DocumentsTab } from "./DocumentsTab";
import { ExpensesTab } from "./ExpensesTab";
import { OverviewBudgetTab } from "./OverviewBudgetTab";
import { ProjectAssetsTab } from "./ProjectAssetsTab";
import { ReportExportTab } from "./ReportExportTab";

/**
 * Project hub (`/projects/:id`, M7 `docs/tasks/M7-project-grants.md`) — the
 * rich per-project destination: Overview/Budget, Assets, Expenses,
 * Documents, Report tabs. `GET /projects/{id}` requires `project.view`
 * (tenant-wide-grantable) for the row itself to 200 at all — a guessed/
 * cross-tenant/other-project id 403s/404s here, handled below as a normal
 * outcome, not a crash (CLAUDE.md).
 */
export function ProjectHubScreen() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { me } = useAuth();

  const [project, setProject] = useState<ProjectDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [forbidden, setForbidden] = useState(false);

  const projectId = id ? Number(id) : NaN;

  const load = useCallback(async () => {
    if (!Number.isFinite(projectId)) {
      setError("Invalid project id.");
      setLoading(false);
      return;
    }
    setLoading(true);
    setError(null);
    setForbidden(false);
    try {
      const detail = await api.getProjectDetail(projectId);
      setProject(detail);
    } catch (err) {
      if (err instanceof ApiError && (err.isForbidden || err.status === 404)) {
        setForbidden(true);
        setError(
          err.status === 404
            ? "This project doesn't exist or you don't have access to it."
            : "You don't have permission to view this project.",
        );
      } else {
        setError(
          err instanceof ApiError
            ? err.problem.detail ?? err.problem.title
            : "Unable to reach the server. Please try again.",
        );
      }
      setProject(null);
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void load();
  }, [load]);

  if (loading) {
    return (
      <AppLayout title="Project" backTo="/projects">
        <Center p="xl">
          <Loader data-testid="project-hub-loading" />
        </Center>
      </AppLayout>
    );
  }

  if (error || !project) {
    return (
      <AppLayout title="Project" backTo="/projects">
        <Alert color={forbidden ? "gray" : "red"} data-testid="project-hub-error">
          <Stack gap="xs" align="flex-start">
            <Text size="sm">{error ?? "Something went wrong."}</Text>
            {!forbidden && (
              <Button size="xs" variant="light" onClick={() => void load()}>
                Retry
              </Button>
            )}
            <Button size="xs" variant="subtle" onClick={() => navigate("/projects")}>
              Back to Projects
            </Button>
          </Stack>
        </Alert>
      </AppLayout>
    );
  }

  const canManage = hasProjectScopedPermission(me, PROJECT_MANAGE, project.id);

  return (
    <AppLayout title={project.name} backTo="/projects">
      <Tabs defaultValue="overview" keepMounted={false} data-testid="project-hub-tabs">
        <Tabs.List>
          <Tabs.Tab value="overview" data-testid="project-tab-overview">
            Overview
          </Tabs.Tab>
          <Tabs.Tab value="assets" data-testid="project-tab-assets">
            Assets
          </Tabs.Tab>
          <Tabs.Tab value="expenses" data-testid="project-tab-expenses">
            Expenses
          </Tabs.Tab>
          <Tabs.Tab value="documents" data-testid="project-tab-documents">
            Documents
          </Tabs.Tab>
          <Tabs.Tab value="report" data-testid="project-tab-report">
            Report
          </Tabs.Tab>
        </Tabs.List>

        <Tabs.Panel value="overview" pt="md">
          <OverviewBudgetTab project={project} canManage={canManage} onUpdated={setProject} />
        </Tabs.Panel>
        <Tabs.Panel value="assets" pt="md">
          <ProjectAssetsTab projectId={project.id} />
        </Tabs.Panel>
        <Tabs.Panel value="expenses" pt="md">
          <ExpensesTab projectId={project.id} />
        </Tabs.Panel>
        <Tabs.Panel value="documents" pt="md">
          <DocumentsTab projectId={project.id} />
        </Tabs.Panel>
        <Tabs.Panel value="report" pt="md">
          <ReportExportTab projectId={project.id} />
        </Tabs.Panel>
      </Tabs>
    </AppLayout>
  );
}
