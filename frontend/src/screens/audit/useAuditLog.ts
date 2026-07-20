import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../../api/client";
import type { AuditLogEntry, AuditLogListParams } from "../../api/types";

const PAGE_SIZE = 25;

interface UseAuditLogOptions {
  /** Caller-owned filter object — stringified so this hook only refetches
   * when the actual filter *content* changes (same pattern as
   * `useStockList`/`useAssetList`). */
  filters: AuditLogListParams;
}

interface UseAuditLogResult {
  items: AuditLogEntry[];
  totalCount: number | null;
  page: number;
  pageCount: number;
  loading: boolean;
  error: string | null;
  /** True specifically for a 403 (`ApiError.isForbidden`) — lets the screen
   * show a distinct "you don't have access" message instead of a generic
   * retry-able error (CLAUDE.md: "a 403 is a normal, handled outcome"). */
  forbidden: boolean;
  setPage: (page: number) => void;
  reload: () => void;
}

/**
 * Server-side paginated audit log list (T5.6, `GET /api/v1/audit`). One
 * bounded page at a time — never "load all audit history" (CLAUDE.md). A
 * filter change resets to page 1. A 403 (caller lacks `audit.view` in any
 * scope) surfaces as a normal handled error state, not a crash — the nav
 * link is already gated presentation-only, but a stale bookmark/direct nav
 * to `/audit` must still degrade gracefully.
 */
export function useAuditLog({ filters }: UseAuditLogOptions): UseAuditLogResult {
  const [items, setItems] = useState<AuditLogEntry[]>([]);
  const [totalCount, setTotalCount] = useState<number | null>(null);
  const [page, setPageState] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [forbidden, setForbidden] = useState(false);

  const requestIdRef = useRef(0);
  const filtersKey = JSON.stringify(filters);

  const fetchPage = useCallback(
    async (targetPage: number) => {
      const requestId = ++requestIdRef.current;
      setLoading(true);
      setError(null);
      setForbidden(false);
      try {
        const body = await api.listAuditLog({ ...filters, page: targetPage, page_size: PAGE_SIZE });
        if (requestId !== requestIdRef.current) return;
        setItems(body.results);
        setTotalCount(body.count);
        setPageState(targetPage);
      } catch (err) {
        if (requestId !== requestIdRef.current) return;
        if (err instanceof ApiError && err.isForbidden) {
          setForbidden(true);
          setError("You don't have permission to view the audit log.");
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

  return { items, totalCount, page, pageCount, loading, error, forbidden, setPage, reload };
}
