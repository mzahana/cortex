import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../../api/client";
import type { Project, ProjectListParams } from "../../api/types";

const PAGE_SIZE = 25;

interface UseProjectListOptions {
  filters: ProjectListParams;
}

interface UseProjectListResult {
  items: Project[];
  totalCount: number | null;
  page: number;
  pageCount: number;
  loading: boolean;
  error: string | null;
  setPage: (page: number) => void;
  reload: () => void;
}

/**
 * Server-side paginated Projects hub list (`GET /api/v1/projects`, M7
 * `docs/tasks/M7-project-grants.md`). One bounded page at a time — never
 * "load all projects" (CLAUDE.md), same pattern as `useAuditLog`. A pure
 * ProjectLead sees only their own project(s) (server-scoped,
 * `apps.projects.api.ProjectViewSet.get_queryset`) — no client-side
 * narrowing needed/possible here.
 */
export function useProjectList({ filters }: UseProjectListOptions): UseProjectListResult {
  const [items, setItems] = useState<Project[]>([]);
  const [totalCount, setTotalCount] = useState<number | null>(null);
  const [page, setPageState] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const requestIdRef = useRef(0);
  const filtersKey = JSON.stringify(filters);

  const fetchPage = useCallback(
    async (targetPage: number) => {
      const requestId = ++requestIdRef.current;
      setLoading(true);
      setError(null);
      try {
        const body = await api.listProjects({ ...filters, page: targetPage, page_size: PAGE_SIZE });
        if (requestId !== requestIdRef.current) return;
        setItems(body.results);
        setTotalCount(body.count);
        setPageState(targetPage);
      } catch (err) {
        if (requestId !== requestIdRef.current) return;
        setError(
          err instanceof ApiError
            ? err.problem.detail ?? err.problem.title
            : "Unable to reach the server. Please try again.",
        );
        setItems([]);
        setTotalCount(null);
      } finally {
        if (requestId === requestIdRef.current) setLoading(false);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [filtersKey],
  );

  useEffect(() => {
    void fetchPage(1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filtersKey]);

  const setPage = useCallback(
    (targetPage: number) => {
      void fetchPage(targetPage);
    },
    [fetchPage],
  );

  const reload = useCallback(() => {
    void fetchPage(page);
  }, [fetchPage, page]);

  const pageCount = totalCount !== null ? Math.max(1, Math.ceil(totalCount / PAGE_SIZE)) : 1;

  return { items, totalCount, page, pageCount, loading, error, setPage, reload };
}
