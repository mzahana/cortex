import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../../api/client";
import type { DashboardSummary } from "../../api/types";

interface UseDashboardSummaryResult {
  summary: DashboardSummary | null;
  loading: boolean;
  error: string | null;
  reload: () => void;
}

/**
 * `GET /api/v1/dashboard/summary` (T5.5/T5.6). Server-side cached (30s TTL +
 * invalidation, `apps.dashboard.services.get_dashboard_summary`) — this hook
 * does no client-side caching of its own, just fetches fresh on mount/reload.
 */
export function useDashboardSummary(): UseDashboardSummaryResult {
  const [summary, setSummary] = useState<DashboardSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const requestIdRef = useRef(0);

  const fetchSummary = useCallback(async () => {
    const requestId = ++requestIdRef.current;
    setLoading(true);
    setError(null);
    try {
      const result = await api.getDashboardSummary();
      if (requestId !== requestIdRef.current) return;
      setSummary(result);
    } catch (err) {
      if (requestId !== requestIdRef.current) return;
      setError(
        err instanceof ApiError
          ? err.problem.detail ?? err.problem.title
          : "Unable to reach the server. Please try again.",
      );
    } finally {
      if (requestId === requestIdRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchSummary();
  }, [fetchSummary]);

  return { summary, loading, error, reload: () => void fetchSummary() };
}
