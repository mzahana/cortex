import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../../api/client";
import type { Membership, MembershipListParams } from "../../api/types";

const PAGE_SIZE = 25;

interface UseMembershipListResult {
  items: Membership[];
  totalCount: number | null;
  page: number;
  pageCount: number;
  loading: boolean;
  error: string | null;
  /** True specifically for a 403 (`ApiError.isForbidden`) — same "handled,
   * not a bug" treatment as `useAuditLog` (CLAUDE.md). */
  forbidden: boolean;
  setPage: (page: number) => void;
  reload: () => void;
}

/**
 * Server-side paginated Membership list (`GET /api/v1/memberships/`) backing
 * the Users & Roles screen's table. An Admin (tenant-wide `user.manage`)
 * sees every tenant Membership; a ProjectLead sees only their own project's
 * (server-enforced, `apps.rbac.api.MembershipViewSet.get_queryset` — no
 * client-side narrowing needed/possible here). Never "loads all" (CLAUDE.md)
 * — one bounded page at a time, same pattern as `useAuditLog`.
 */
export function useMembershipList(): UseMembershipListResult {
  const [items, setItems] = useState<Membership[]>([]);
  const [totalCount, setTotalCount] = useState<number | null>(null);
  const [page, setPageState] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [forbidden, setForbidden] = useState(false);

  const requestIdRef = useRef(0);

  const fetchPage = useCallback(async (targetPage: number) => {
    const requestId = ++requestIdRef.current;
    setLoading(true);
    setError(null);
    setForbidden(false);
    try {
      const params: MembershipListParams = {
        page: targetPage,
        page_size: PAGE_SIZE,
        ordering: "-created_at",
      };
      const body = await api.listMemberships(params);
      if (requestId !== requestIdRef.current) return;
      setItems(body.results);
      setTotalCount(body.count);
      setPageState(targetPage);
    } catch (err) {
      if (requestId !== requestIdRef.current) return;
      if (err instanceof ApiError && err.isForbidden) {
        setForbidden(true);
        setError("You don't have permission to view members.");
      } else {
        setError(
          err instanceof ApiError
            ? err.problem.detail ?? err.problem.title
            : "Unable to reach the server. Please try again.",
        );
      }
      setItems([]);
      setTotalCount(null);
    } finally {
      if (requestId === requestIdRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchPage(1);
  }, [fetchPage]);

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

  return { items, totalCount, page, pageCount, loading, error, forbidden, setPage, reload };
}
